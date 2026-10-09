"""Durable Currency trade approvals; no networking or runtime activation.

Reconstructed from the agreed interface after a workspace outage. This version
must pass the isolated CI database tests before it is used. It does not inherit
paper/news permissions and does not call a broker or Telegram.

Supply a psycopg3-style connect callable returning dict rows and a stable secret
of at least 32 bytes. Every mutation owns an explicit transaction, including when
the supplied connection has autocommit=True. Schema creation is explicit.
Lock order is account/instrument scope, then proposal; a caller combining this
repository with a ledger must acquire the ledger account lock afterwards.

Terms and recipient identities are immutable and HMAC-bound. A callback only
records the owner's decision. A separate, revalidating coordinator may claim an
approved proposal exactly once before attempting an external order. SENDING
expires to UNKNOWN, never back to APPROVED. Terminal executions retain their
instrument scope until actual fills and known fees have been reconciled.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import secrets
import uuid

TABLE = "veritas_trade_approvals"
SCOPES = "veritas_trade_approval_scopes"
CALLBACKS = "veritas_trade_approval_callbacks"
AUDIT = "veritas_trade_approval_audit"
EXIT_FAMILIES = "veritas_trade_approval_exit_families"
MAX_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 120
SEND_TIMEOUT_SECONDS = 120
DELIVERY_TIMEOUT_SECONDS = 60
UTC = timezone.utc

PRE_SUBMISSION = frozenset({
    "PENDING_DELIVERY", "DELIVERY_SENDING", "DELIVERY_UNKNOWN",
    "AWAITING_OWNER", "APPROVED",
})
TERMINAL_EXECUTION = frozenset({"FILLED", "CANCELLED", "BROKER_REJECTED"})
UNSETTLED_EXECUTION = frozenset({
    "SENDING", "UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED",
})
STATUSES = PRE_SUBMISSION | TERMINAL_EXECUTION | UNSETTLED_EXECUTION | {
    "EXPIRED", "REJECTED", "BLOCKED",
}
MUTABLE_COLUMNS = frozenset({
    "status", "updated_at", "telegram_message_id", "delivered_at",
    "delivery_token", "delivery_started_at", "delivery_deadline",
    "approved_at", "approved_by", "claim_token", "worker_id",
    "send_started_at", "send_deadline", "broker_order_id", "broker_status",
    "filled_lots", "average_fill_price", "broker_observed_at",
    "execution_reconciled", "reason_code",
})
DATE_COLUMNS = frozenset({
    "created_at", "expires_at", "updated_at", "delivered_at",
    "delivery_started_at", "delivery_deadline", "approved_at",
    "send_started_at", "send_deadline", "broker_observed_at",
})
_CALLBACK = re.compile(r"^ta:([ar]):([a-f0-9]{32}):([A-Za-z0-9_-]{22})$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


class ApprovalError(ValueError):
    def __init__(self, code, status_code=409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def utcnow():
    return datetime.now(UTC)


def _date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ApprovalError("INVALID_TIMESTAMP", 400) from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalError("TIMEZONE_AWARE_TIMESTAMP_REQUIRED", 400)
    return value.astimezone(UTC)


def _iso(value):
    return _date(value).isoformat()


def _positive_id(value):
    if type(value) is not int or value <= 0 or value > 9223372036854775807:
        raise ApprovalError("POSITIVE_INTEGER_ID_REQUIRED", 400)
    return value


def _text(value, name="VALUE", maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ApprovalError("INVALID_" + name, 400)
    if any(ord(x) < 32 for x in value) or value != value.strip():
        raise ApprovalError("INVALID_" + name, 400)
    return value


def _reason(value):
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ApprovalError("INVALID_REASON_CODE", 400)
    return value


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or isinstance(value, float):
        raise ApprovalError("FLOAT_NOT_ALLOWED", 400)
    if not isinstance(value, (Decimal, int, str)):
        raise ApprovalError("INVALID_DECIMAL", 400)
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ApprovalError("INVALID_DECIMAL", 400) from None
    if not result.is_finite() or abs(result.adjusted()) > 100:
        raise ApprovalError("NONFINITE_OR_EXCESSIVE_DECIMAL", 400)
    if positive and result <= 0:
        raise ApprovalError("POSITIVE_PRICE_REQUIRED", 400)
    return result


def _decimal_string(value):
    value = _decimal(value)
    if value == 0:
        return "0"
    raw = format(value, "f")
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ApprovalError("DUPLICATE_JSON_KEY", 400)
        result[key] = value
    return result


def _invalid_constant(_):
    raise ApprovalError("NONFINITE_JSON_NUMBER", 400)


def _normalise(value, depth=0):
    if depth > 16:
        raise ApprovalError("TERMS_TOO_DEEP", 400)
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, float):
        raise ApprovalError("FLOAT_NOT_ALLOWED", 400)
    if isinstance(value, list):
        return [_normalise(x, depth + 1) for x in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ApprovalError("JSON_OBJECT_KEYS_REQUIRED", 400)
        return {k: _normalise(v, depth + 1) for k, v in value.items()}
    raise ApprovalError("STRICT_JSON_TERMS_REQUIRED", 400)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_terms(value):
    """Return a fresh strict-JSON object with Decimal values encoded as strings.

    Extra economics/source/account fields are preserved in the signed hash.
    Binary floats are rejected, even in nested auxiliary data. Fractional JSON
    literals are read directly as Decimal, without a binary floating-point step.
    """
    if isinstance(value, (str, bytes, bytearray)):
        if len(value) > 65536:
            raise ApprovalError("TERMS_TOO_LARGE", 400)
        try:
            value = json.loads(value, parse_float=Decimal,
                               parse_constant=_invalid_constant,
                               object_pairs_hook=_strict_pairs)
        except (ValueError, TypeError, UnicodeError) as exc:
            if isinstance(exc, ApprovalError):
                raise
            raise ApprovalError("INVALID_TERMS_JSON", 400) from None
    if not isinstance(value, dict):
        raise ApprovalError("TERMS_OBJECT_REQUIRED", 400)
    result = _normalise(value)
    if len(_json(result).encode("utf-8")) > 65536:
        raise ApprovalError("TERMS_TOO_LARGE", 400)
    for key in ("account_id", "instrument_uid", "canonical_event_id"):
        result[key] = _text(result.get(key), key.upper())
    if result.get("asset") != "CNYRUBF":
        raise ApprovalError("CNYRUBF_REQUIRED", 400)
    action, direction = result.get("action"), result.get("direction")
    if action not in {"OPEN", "ADD", "REDUCE", "CLOSE"}:
        raise ApprovalError("INVALID_ACTION", 400)
    if direction not in {"LONG", "SHORT"}:
        raise ApprovalError("INVALID_DIRECTION", 400)
    lots = result.get("lots")
    if type(lots) is not int or lots <= 0 or lots > 2147483647:
        raise ApprovalError("POSITIVE_INTEGER_LOTS_REQUIRED", 400)
    entry = action in {"OPEN", "ADD"}
    side = "BUY" if (direction == "LONG") == entry else "SELL"
    if result.get("side", side) != side:
        raise ApprovalError("DIRECTION_SIDE_MISMATCH", 400)
    result["side"] = side
    if result.get("order_type", "LIMIT") != "LIMIT":
        raise ApprovalError("LIMIT_ORDER_REQUIRED", 400)
    result["order_type"] = "LIMIT"
    price = _decimal(result.get("limit_price"), positive=True)
    result["limit_price"] = _decimal_string(price)
    for key in ("stop_price", "target_price"):
        if result.get(key) is not None:
            result[key] = _decimal_string(_decimal(result[key], positive=True))
    if entry:
        stop = _decimal(result.get("stop_price"), positive=True)
        target = _decimal(result.get("target_price"), positive=True)
        valid = stop < price < target if direction == "LONG" else target < price < stop
        if not valid:
            raise ApprovalError("INVALID_ENTRY_GEOMETRY", 400)
    return result


def ensure_schema(c):
    """Create only the isolated approval tables; caller owns the transaction."""
    statements = [
        f"""CREATE TABLE IF NOT EXISTS {SCOPES} (
            account_id TEXT NOT NULL,
            instrument_uid TEXT NOT NULL,
            PRIMARY KEY (account_id, instrument_uid)
        )""",
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            proposal_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            instrument_uid TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('OPEN','ADD','REDUCE','CLOSE')),
            direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
            lots INTEGER NOT NULL CHECK (lots > 0),
            terms_json JSONB NOT NULL,
            terms_hash TEXT NOT NULL,
            terms_signature TEXT NOT NULL,
            economics_revision INTEGER NOT NULL,
            owner_user_id BIGINT NOT NULL CHECK (owner_user_id > 0),
            private_chat_id BIGINT NOT NULL CHECK (private_chat_id = owner_user_id),
            bot_id BIGINT NOT NULL CHECK (bot_id > 0),
            callback_nonce TEXT NOT NULL,
            client_order_id TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            telegram_message_id BIGINT,
            delivered_at TIMESTAMPTZ,
            delivery_token TEXT,
            delivery_started_at TIMESTAMPTZ,
            delivery_deadline TIMESTAMPTZ,
            approved_at TIMESTAMPTZ,
            approved_by BIGINT,
            claim_token TEXT,
            worker_id TEXT,
            send_started_at TIMESTAMPTZ,
            send_deadline TIMESTAMPTZ,
            broker_order_id TEXT,
            broker_status TEXT,
            filled_lots INTEGER,
            average_fill_price TEXT,
            broker_observed_at TIMESTAMPTZ,
            execution_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
            reason_code TEXT,
            UNIQUE (account_id, canonical_event_id, action),
            CHECK (filled_lots IS NULL OR (filled_lots >= 0 AND filled_lots <= lots))
        )""",
        f"""CREATE TABLE IF NOT EXISTS {EXIT_FAMILIES} (
            account_id TEXT NOT NULL,
            instrument_uid TEXT NOT NULL,
            root_exit_event_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('REDUCE','CLOSE')),
            generation INTEGER NOT NULL CHECK (generation >= 0),
            latest_proposal_id TEXT NOT NULL REFERENCES {TABLE}(proposal_id),
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (account_id, instrument_uid, root_exit_event_id, action)
        )""",
        f"""CREATE UNIQUE INDEX IF NOT EXISTS veritas_trade_active_instrument
            ON {TABLE} (account_id, instrument_uid)
            WHERE status IN ('APPROVED','SENDING','UNKNOWN','ACKNOWLEDGED','PARTIALLY_FILLED')
               OR (status IN ('FILLED','CANCELLED','BROKER_REJECTED')
                   AND execution_reconciled = FALSE)""",
        f"""CREATE UNIQUE INDEX IF NOT EXISTS veritas_trade_broker_order
            ON {TABLE} (account_id, broker_order_id) WHERE broker_order_id IS NOT NULL""",
        f"""CREATE INDEX IF NOT EXISTS veritas_trade_approval_status
            ON {TABLE} (status, created_at, proposal_id)""",
        f"""CREATE TABLE IF NOT EXISTS {CALLBACKS} (
            callback_query_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            action TEXT NOT NULL,
            terms_hash TEXT NOT NULL,
            sender_user_id BIGINT NOT NULL,
            private_chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            bot_id BIGINT NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL,
            decision_status TEXT NOT NULL,
            decision_signature TEXT NOT NULL,
            UNIQUE (proposal_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS {AUDIT} (
            id BIGSERIAL PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            at TIMESTAMPTZ NOT NULL,
            details_json JSONB NOT NULL
        )""",
    ]
    for statement in statements:
        c.execute(statement)


class TradeApprovals:
    """Durable proposal repository. Returned records never expose HMAC secrets.

    Public records include immutable terms/terms_hash and recipient identities,
    proposal/client order IDs, status, delivery and send tokens/times, broker
    observation state, execution_reconciled, and callbacks{approve,reject}.
    Times are ISO UTC and exact decimal amounts are strings.
    """

    def __init__(self, pg_connect, signing_key, *, clock=utcnow):
        if isinstance(signing_key, str):
            signing_key = signing_key.encode("utf-8")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ApprovalError("SIGNING_KEY_TOO_SHORT", 400)
        if not callable(pg_connect) or not callable(clock):
            raise ApprovalError("CONNECT_AND_CLOCK_REQUIRED", 400)
        self.connect = pg_connect
        self.key = signing_key
        self.clock = clock

    def _now(self):
        return _date(self.clock())

    @contextmanager
    def _transaction(self):
        with self.connect() as c:
            with c.transaction():
                yield c

    def ensure_schema(self):
        with self._transaction() as c:
            ensure_schema(c)

    def _signature(self, domain, value):
        return hmac.new(self.key, (domain + "\n" + _json(value)).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def _envelope(self, row):
        keys = (
            "proposal_id", "schema_version", "account_id", "instrument_uid",
            "canonical_event_id", "action", "direction", "lots", "terms_hash",
            "economics_revision", "owner_user_id", "private_chat_id", "bot_id",
            "client_order_id", "callback_nonce", "created_at", "expires_at",
        )
        return {k: _iso(row[k]) if k in DATE_COLUMNS else row[k] for k in keys}

    def _verify(self, row):
        if row is None:
            raise ApprovalError("PROPOSAL_NOT_FOUND", 404)
        row = dict(row)
        try:
            terms = canonical_terms(row["terms_json"])
            digest = hashlib.sha256(_json(terms).encode("utf-8")).hexdigest()
            signature = self._signature("veritas-trade-terms-v1", self._envelope(row))
            same_scope = all(row[k] == terms[k] for k in (
                "account_id", "instrument_uid", "canonical_event_id",
                "action", "direction", "lots",
            ))
            valid = (same_scope and row["status"] in STATUSES
                     and hmac.compare_digest(digest, row["terms_hash"])
                     and hmac.compare_digest(signature, row["terms_signature"]))
        except (ApprovalError, ValueError, TypeError, KeyError):
            raise ApprovalError("TERMS_INTEGRITY_FAILED") from None
        if not valid:
            raise ApprovalError("TERMS_INTEGRITY_FAILED")
        row["terms_json"] = terms
        return row

    def _tag(self, row, action):
        raw = hmac.new(self.key, ("veritas-trade-callback-v1\n" + action + "\n"
                       + row["proposal_id"] + "\n" + row["terms_signature"]
                       + "\n" + row["callback_nonce"]).encode("utf-8"),
                       hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _view(self, row, **extra):
        row = self._verify(row)
        result = {k: (_iso(v) if k in DATE_COLUMNS and v is not None else v)
                  for k, v in row.items()
                  if k not in {"terms_json", "terms_signature", "callback_nonce"}}
        result["terms"] = row["terms_json"]
        result["execution_reconciled"] = bool(result["execution_reconciled"])
        result["callbacks"] = {
            "approve": "ta:a:" + row["proposal_id"] + ":" + self._tag(row, "a"),
            "reject": "ta:r:" + row["proposal_id"] + ":" + self._tag(row, "r"),
        }
        result.update(extra)
        return result

    def _row(self, c, proposal_id, *, lock=False, skip_locked=False):
        _text(proposal_id, "PROPOSAL_ID", 64)
        suffix = " FOR UPDATE" if lock else ""
        if lock and skip_locked:
            suffix += " SKIP LOCKED"
        return c.execute(f"SELECT * FROM {TABLE} WHERE proposal_id = %s" + suffix,
                         (proposal_id,)).fetchone()

    def _scope(self, c, account_id, instrument_uid, *, skip_locked=False):
        if not skip_locked:
            c.execute(f"""INSERT INTO {SCOPES} (account_id, instrument_uid)
                VALUES (%s,%s) ON CONFLICT (account_id,instrument_uid) DO NOTHING""",
                      (account_id, instrument_uid))
        suffix = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
        return c.execute(f"""SELECT account_id FROM {SCOPES}
            WHERE account_id=%s AND instrument_uid=%s""" + suffix,
                         (account_id, instrument_uid)).fetchone() is not None

    def _locked(self, c, proposal_id, *, skip_locked=False):
        before = self._verify(self._row(c, proposal_id))
        if not self._scope(c, before["account_id"], before["instrument_uid"],
                           skip_locked=skip_locked):
            return None
        row = self._row(c, proposal_id, lock=True, skip_locked=skip_locked)
        return self._verify(row) if row is not None else None

    def _audit(self, c, row, event, old_status=None, details=None):
        c.execute(f"""INSERT INTO {AUDIT}
            (proposal_id,event_type,from_status,to_status,at,details_json)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                  (row["proposal_id"], event, old_status, row["status"],
                   self._now(), _json(details or {})))

    def _change(self, c, row, changes, event, details=None):
        if not set(changes) <= MUTABLE_COLUMNS:
            raise ApprovalError("IMMUTABLE_COLUMN_UPDATE_FORBIDDEN")
        changes = dict(changes, updated_at=self._now())
        assignments = ", ".join(k + "=%s" for k in changes)
        c.execute(f"UPDATE {TABLE} SET {assignments} WHERE proposal_id=%s",
                  tuple(changes.values()) + (row["proposal_id"],))
        updated = self._verify(self._row(c, row["proposal_id"]))
        self._audit(c, updated, event, row["status"], details)
        return updated

    def _expire_row(self, c, row):
        now, state = self._now(), row["status"]
        if state in PRE_SUBMISSION and now >= _date(row["expires_at"]):
            return self._change(c, row, {"status": "EXPIRED", "reason_code": "PROPOSAL_EXPIRED"},
                                "EXPIRED")
        if state == "DELIVERY_SENDING" and row["delivery_deadline"] is not None:
            if now >= _date(row["delivery_deadline"]):
                return self._change(c, row, {"status": "DELIVERY_UNKNOWN",
                                            "reason_code": "DELIVERY_OUTCOME_UNKNOWN"},
                                    "DELIVERY_TIMEOUT")
        if state == "SENDING" and row["send_deadline"] is not None:
            if now >= _date(row["send_deadline"]):
                return self._change(c, row, {"status": "UNKNOWN",
                                            "reason_code": "BROKER_OUTCOME_UNKNOWN"},
                                    "SUBMISSION_TIMEOUT")
        return row

    def _prepare_creation(self, terms, *, owner_user_id, private_chat_id, bot_id,
                          expires_at=None, economics_revision=1):
        terms = canonical_terms(terms)
        owner_user_id, private_chat_id, bot_id = map(
            _positive_id, (owner_user_id, private_chat_id, bot_id))
        if owner_user_id != private_chat_id:
            raise ApprovalError("OWNER_PRIVATE_CHAT_REQUIRED", 400)
        if type(economics_revision) is not int or economics_revision <= 0:
            raise ApprovalError("POSITIVE_ECONOMICS_REVISION_REQUIRED", 400)
        now = self._now()
        expiry = (_date(expires_at) if expires_at is not None
                  else now + timedelta(seconds=DEFAULT_TTL_SECONDS))
        if not now < expiry <= now + timedelta(seconds=MAX_TTL_SECONDS):
            raise ApprovalError("INVALID_PROPOSAL_EXPIRY", 400)
        digest = hashlib.sha256(_json(terms).encode("utf-8")).hexdigest()
        row = {k: terms[k] for k in (
            "account_id", "instrument_uid", "canonical_event_id", "action", "direction", "lots",
        )}
        row.update(
            proposal_id=uuid.uuid4().hex, schema_version=1, terms_json=terms,
            terms_hash=digest, economics_revision=economics_revision,
            owner_user_id=owner_user_id, private_chat_id=private_chat_id, bot_id=bot_id,
            callback_nonce=secrets.token_urlsafe(16), client_order_id=str(uuid.uuid4()),
            created_at=now, expires_at=expiry, updated_at=now, status="PENDING_DELIVERY",
        )
        row["terms_signature"] = self._signature("veritas-trade-terms-v1", self._envelope(row))
        return row

    def _insert_created(self, c, row):
        """Insert within the caller's already-held scope transaction."""
        columns = list(row)
        values = [(_json(row[k]) if k == "terms_json" else row[k]) for k in columns]
        placeholders = ["%s::jsonb" if k == "terms_json" else "%s" for k in columns]
        inserted = c.execute(
            f"INSERT INTO {TABLE} ({','.join(columns)}) VALUES ({','.join(placeholders)}) "
            "ON CONFLICT (account_id,canonical_event_id,action) DO NOTHING RETURNING *",
            tuple(values),
        ).fetchone()
        if inserted is None:
            existing = self._verify(c.execute(
                f"SELECT * FROM {TABLE} WHERE account_id=%s AND canonical_event_id=%s AND action=%s",
                (row["account_id"], row["canonical_event_id"], row["action"]),
            ).fetchone())
            fields = ("terms_hash", "economics_revision", "owner_user_id", "private_chat_id", "bot_id")
            if any(existing[k] != row[k] for k in fields):
                raise ApprovalError("EVENT_TERMS_CONFLICT")
            return self._view(existing, idempotent=True)
        inserted = self._verify(inserted)
        self._audit(c, inserted, "CREATED")
        return self._view(inserted, idempotent=False)

    def create(self, terms, *, owner_user_id, private_chat_id, bot_id,
               expires_at=None, economics_revision=1):
        row = self._prepare_creation(
            terms, owner_user_id=owner_user_id, private_chat_id=private_chat_id,
            bot_id=bot_id, expires_at=expires_at, economics_revision=economics_revision)
        with self._transaction() as c:
            self._scope(c, row["account_id"], row["instrument_uid"])
            return self._insert_created(c, row)

    def _exit_event_id(self, family, generation):
        if generation == 0:
            return family["root_exit_event_id"]
        identity = {k: family[k] for k in (
            "account_id", "instrument_uid", "root_exit_event_id", "action")}
        identity["generation"] = generation
        return "EXIT-RENEW-" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()

    def _verify_exit_family(self, c, family, row):
        generation = family["generation"]
        terms = row["terms_json"]
        if (type(generation) is not int or generation < 0
                or row["account_id"] != family["account_id"]
                or row["instrument_uid"] != family["instrument_uid"]
                or row["action"] != family["action"]
                or row["canonical_event_id"] != self._exit_event_id(family, generation)):
            raise ApprovalError("EXIT_FAMILY_INTEGRITY_FAILED")
        metadata = {"root_exit_event_id", "parent_proposal_id", "exit_generation"}
        if generation == 0 and not metadata.intersection(terms):
            return  # Adopt a legacy, immutable first proposal without editing it.
        if (terms.get("root_exit_event_id") != family["root_exit_event_id"]
                or type(terms.get("exit_generation")) is not int
                or terms["exit_generation"] != generation):
            raise ApprovalError("EXIT_FAMILY_INTEGRITY_FAILED")
        if generation == 0:
            if terms.get("parent_proposal_id") is not None:
                raise ApprovalError("EXIT_FAMILY_INTEGRITY_FAILED")
            return
        previous = c.execute(f"""SELECT * FROM {TABLE}
            WHERE account_id=%s AND canonical_event_id=%s AND action=%s""",
            (family["account_id"], self._exit_event_id(family, generation - 1),
             family["action"])).fetchone()
        previous = self._verify(previous)
        if (previous["instrument_uid"] != family["instrument_uid"]
                or terms.get("parent_proposal_id") != previous["proposal_id"]):
            raise ApprovalError("EXIT_FAMILY_INTEGRITY_FAILED")

    def create_exit_successor(self, terms, *, owner_user_id, private_chat_id,
                              bot_id, expires_at=None, economics_revision=1):
        """Create/reuse an immutable protective-exit proposal family.

        Call this for the first exit as well as refreshes. Input canonical_event_id
        must be the stable root exit event (for example entry+ledger revision+exit
        reason), without repository-owned family metadata. Current pending or
        unsettled proposals are returned unchanged, including their price/TTL.

        A successor is permitted only after EXPIRED/BLOCKED without any broker
        send, or a reconciled CANCELLED/BROKER_REJECTED with exactly zero fills.
        Owner rejection, UNKNOWN, active broker work and unaccounted terminal
        results never create a successor. Each permitted generation requires a
        fresh owner approval and has new signed terms, callback and request UUID.
        """
        candidate = self._prepare_creation(
            terms, owner_user_id=owner_user_id, private_chat_id=private_chat_id,
            bot_id=bot_id, expires_at=expires_at, economics_revision=economics_revision)
        if candidate["action"] not in {"REDUCE", "CLOSE"}:
            raise ApprovalError("EXIT_ACTION_REQUIRED", 400)
        base_terms = candidate["terms_json"]
        if {"root_exit_event_id", "parent_proposal_id", "exit_generation"}.intersection(base_terms):
            raise ApprovalError("EXIT_FAMILY_METADATA_IS_REPOSITORY_OWNED", 400)
        family = {k: candidate[k] for k in ("account_id", "instrument_uid", "action")}
        family["root_exit_event_id"] = candidate["canonical_event_id"]
        scope = tuple(family[k] for k in (
            "account_id", "instrument_uid", "root_exit_event_id", "action"))
        def generation_row(number, parent_id):
            value = dict(
                base_terms, canonical_event_id=self._exit_event_id(family, number),
                root_exit_event_id=family["root_exit_event_id"],
                parent_proposal_id=parent_id, exit_generation=number)
            return self._prepare_creation(
                value, owner_user_id=owner_user_id, private_chat_id=private_chat_id,
                bot_id=bot_id, expires_at=candidate["expires_at"],
                economics_revision=economics_revision)
        with self._transaction() as c:
            self._scope(c, candidate["account_id"], candidate["instrument_uid"])
            stored = c.execute(f"""SELECT * FROM {EXIT_FAMILIES}
                WHERE account_id=%s AND instrument_uid=%s AND root_exit_event_id=%s AND action=%s
                FOR UPDATE""", scope).fetchone()
            if stored is None:
                legacy = c.execute(f"""SELECT * FROM {TABLE}
                    WHERE account_id=%s AND canonical_event_id=%s AND action=%s FOR UPDATE""",
                    (candidate["account_id"], family["root_exit_event_id"],
                     candidate["action"])).fetchone()
                if legacy is None:
                    first = self._insert_created(c, generation_row(0, None))
                    c.execute(f"""INSERT INTO {EXIT_FAMILIES}
                        (account_id,instrument_uid,root_exit_event_id,action,generation,
                         latest_proposal_id,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        scope + (0, first["proposal_id"], self._now()))
                    return first
                current = self._verify(legacy)
                if current["instrument_uid"] != candidate["instrument_uid"]:
                    raise ApprovalError("EXIT_FAMILY_IDENTITY_MISMATCH")
                stored = dict(family, generation=0, latest_proposal_id=current["proposal_id"])
                c.execute(f"""INSERT INTO {EXIT_FAMILIES}
                    (account_id,instrument_uid,root_exit_event_id,action,generation,
                     latest_proposal_id,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    scope + (0, current["proposal_id"], self._now()))
            else:
                stored = dict(stored)
                current = self._verify(self._row(c, stored["latest_proposal_id"], lock=True))
            self._verify_exit_family(c, stored, current)
            for key in ("account_id", "instrument_uid", "action", "direction",
                        "owner_user_id", "private_chat_id", "bot_id"):
                if current[key] != candidate[key]:
                    raise ApprovalError("EXIT_FAMILY_IDENTITY_MISMATCH")
            if current["terms_json"].get("execution_environment") != base_terms.get("execution_environment"):
                raise ApprovalError("EXIT_FAMILY_IDENTITY_MISMATCH")
            current = self._expire_row(c, current)
            state = current["status"]
            unsent = not current["claim_token"] and not current["send_started_at"]
            renewable = (state in {"EXPIRED", "BLOCKED"} and unsent) or (
                state in {"CANCELLED", "BROKER_REJECTED"}
                and current["execution_reconciled"] and current["filled_lots"] == 0)
            if not renewable:
                return self._view(current, idempotent=True,
                                  renewal_blocked=state not in PRE_SUBMISSION)
            generation = stored["generation"] + 1
            child = self._insert_created(c, generation_row(generation, current["proposal_id"]))
            c.execute(f"""UPDATE {EXIT_FAMILIES}
                SET generation=%s,latest_proposal_id=%s,updated_at=%s
                WHERE account_id=%s AND instrument_uid=%s AND root_exit_event_id=%s AND action=%s""",
                (generation, child["proposal_id"], self._now()) + scope)
            self._audit(c, self._verify(self._row(c, child["proposal_id"])),
                        "EXIT_SUCCESSOR_CREATED",
                        details={"parent_proposal_id": current["proposal_id"],
                                 "root_exit_event_id": family["root_exit_event_id"],
                                 "exit_generation": generation})
            return child

    def get(self, proposal_id):
        with self.connect() as c:
            return self._view(self._row(c, proposal_id))

    def get_by_event(self, account_id, event_id, action):
        """Retry a specific immutable intent without renewing its terms or expiry."""
        with self.connect() as c:
            row = c.execute(f"SELECT * FROM {TABLE} WHERE account_id=%s AND canonical_event_id=%s AND action=%s",
                            (account_id, event_id, action)).fetchone()
            return self._view(row) if row is not None else None

    def claim_delivery(self, proposal_id, worker_id):
        """Commit the send barrier before Telegram. Never reclaim this send."""
        worker_id = _text(worker_id, "WORKER_ID", 128)
        with self._transaction() as c:
            row = self._locked(c, proposal_id, skip_locked=True)
            if row is None:
                return None
            row = self._expire_row(c, row)
            if row["status"] != "PENDING_DELIVERY":
                return None
            now = self._now()
            row = self._change(c, row, {
                "status": "DELIVERY_SENDING", "delivery_token": secrets.token_urlsafe(24),
                "delivery_started_at": now,
                "delivery_deadline": min(_date(row["expires_at"]),
                                         now + timedelta(seconds=DELIVERY_TIMEOUT_SECONDS)),
                "worker_id": worker_id,
            }, "DELIVERY_CLAIMED")
            return self._view(row)

    def _check_delivery_token(self, row, delivery_token):
        if (not isinstance(delivery_token, str) or not row["delivery_token"]
                or not hmac.compare_digest(delivery_token, row["delivery_token"])):
            raise ApprovalError("DELIVERY_TOKEN_MISMATCH", 403)

    def record_delivery_unknown(self, proposal_id, delivery_token):
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            self._check_delivery_token(row, delivery_token)
            row = self._expire_row(c, row)
            if row["telegram_message_id"] is not None:
                return self._view(row, idempotent=True)
            if row["status"] in {"DELIVERY_UNKNOWN", "EXPIRED", "BLOCKED"}:
                return self._view(row, idempotent=True)
            if row["status"] != "DELIVERY_SENDING":
                raise ApprovalError("DELIVERY_NOT_CLAIMED")
            row = self._change(c, row, {
                "status": "DELIVERY_UNKNOWN", "reason_code": "DELIVERY_OUTCOME_UNKNOWN",
            }, "DELIVERY_UNKNOWN")
            return self._view(row)

    def mark_delivered(self, proposal_id, *, bot_id, private_chat_id, message_id,
                       terms_hash, delivery_token=None):
        bot_id, private_chat_id, message_id = map(
            _positive_id, (bot_id, private_chat_id, message_id))
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            self._check_delivery_token(row, delivery_token)
            if (row["bot_id"] != bot_id or row["private_chat_id"] != private_chat_id
                    or not isinstance(terms_hash, str)
                    or not hmac.compare_digest(row["terms_hash"], terms_hash)):
                raise ApprovalError("DELIVERY_BINDING_MISMATCH", 403)
            row = self._expire_row(c, row)
            if row["telegram_message_id"] is not None:
                if row["telegram_message_id"] != message_id:
                    raise ApprovalError("DELIVERY_MESSAGE_ALREADY_BOUND")
                return self._view(row, idempotent=True,
                                  accepted=row["status"] not in {"EXPIRED", "BLOCKED"})
            if row["status"] == "EXPIRED":
                return self._view(row, accepted=False, reason_code="PROPOSAL_EXPIRED")
            if row["status"] not in {"DELIVERY_SENDING", "DELIVERY_UNKNOWN"}:
                raise ApprovalError("DELIVERY_NOT_CLAIMED")
            row = self._change(c, row, {
                "telegram_message_id": message_id, "delivered_at": self._now(),
                "status": "AWAITING_OWNER", "reason_code": None,
            }, "DELIVERED")
            return self._view(row, accepted=True)

    def _decision_payload(self, decision):
        keys = (
            "callback_query_id", "proposal_id", "action", "terms_hash",
            "sender_user_id", "private_chat_id", "message_id", "bot_id",
            "decided_at", "decision_status",
        )
        return {k: _iso(decision[k]) if k == "decided_at" else decision[k] for k in keys}

    def _verify_decision(self, decision):
        try:
            expected = self._signature("veritas-trade-owner-decision-v1",
                                       self._decision_payload(decision))
            valid = hmac.compare_digest(expected, decision["decision_signature"])
        except (ApprovalError, KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ApprovalError("CALLBACK_LEDGER_INTEGRITY_FAILED")
        return decision

    def _has_active(self, c, row):
        return c.execute(f"""SELECT proposal_id FROM {TABLE}
            WHERE account_id=%s AND instrument_uid=%s AND proposal_id<>%s
              AND (status IN ('APPROVED','SENDING','UNKNOWN','ACKNOWLEDGED','PARTIALLY_FILLED')
                OR (status IN ('FILLED','CANCELLED','BROKER_REJECTED')
                    AND execution_reconciled=FALSE))
            LIMIT 1""", (row["account_id"], row["instrument_uid"], row["proposal_id"])
                         ).fetchone() is not None

    def decide(self, callback_data, *, sender_user_id, private_chat_id,
               message_id, bot_id, callback_query_id):
        """Persist an authenticated owner decision, never execute an order."""
        match = _CALLBACK.fullmatch(callback_data) if isinstance(callback_data, str) else None
        if match is None:
            raise ApprovalError("INVALID_TRADE_CALLBACK", 400)
        action, proposal_id, tag = match.groups()
        sender_user_id, private_chat_id, message_id, bot_id = map(
            _positive_id, (sender_user_id, private_chat_id, message_id, bot_id))
        callback_query_id = _text(callback_query_id, "CALLBACK_QUERY_ID", 256)
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            if (row["owner_user_id"] != sender_user_id
                    or row["private_chat_id"] != private_chat_id
                    or row["telegram_message_id"] != message_id
                    or row["bot_id"] != bot_id):
                raise ApprovalError("CALLBACK_OWNER_MESSAGE_BINDING_MISMATCH", 403)
            if not hmac.compare_digest(tag, self._tag(row, action)):
                raise ApprovalError("CALLBACK_SIGNATURE_INVALID", 403)
            row = self._expire_row(c, row)
            existing = c.execute(
                f"SELECT * FROM {CALLBACKS} WHERE callback_query_id=%s",
                (callback_query_id,),
            ).fetchone()
            expected = {
                "proposal_id": proposal_id, "action": action, "terms_hash": row["terms_hash"],
                "sender_user_id": sender_user_id, "private_chat_id": private_chat_id,
                "message_id": message_id, "bot_id": bot_id,
            }
            if existing is not None:
                existing = self._verify_decision(dict(existing))
                if any(existing[k] != value for k, value in expected.items()):
                    raise ApprovalError("CALLBACK_REPLAY_CONFLICT", 403)
                return self._view(row, idempotent=True, accepted=True,
                                  decision_status=existing["decision_status"])
            if row["status"] != "AWAITING_OWNER":
                return self._view(row, accepted=False,
                                  reason_code="DECISION_ALREADY_FINAL_OR_EXPIRED")
            if action == "a" and self._has_active(c, row):
                return self._view(row, accepted=False,
                                  reason_code="INSTRUMENT_HAS_UNSETTLED_EXECUTION")
            now = self._now()
            state = "APPROVED" if action == "a" else "REJECTED"
            decision = dict(expected, callback_query_id=callback_query_id,
                            decided_at=now, decision_status=state)
            decision["decision_signature"] = self._signature(
                "veritas-trade-owner-decision-v1", self._decision_payload(decision))
            keys = list(decision)
            # Signed decision and status change are one transaction. A failed
            # audit or duplicate query cannot leave an unsigned APPROVED row.
            c.execute(f"INSERT INTO {CALLBACKS} ({','.join(keys)}) "
                      f"VALUES ({','.join('%s' for _ in keys)})",
                      tuple(decision[k] for k in keys))
            row = self._change(c, row, {
                "status": state, "approved_at": now if action == "a" else None,
                "approved_by": sender_user_id if action == "a" else None,
                "reason_code": "OWNER_REJECTED" if action == "r" else None,
            }, "OWNER_APPROVED" if action == "a" else "OWNER_REJECTED",
                {"callback_query_id": callback_query_id})
            return self._view(row, accepted=True, decision_status=state)

    def auto_approve_sandbox(self, proposal_id):
        """Approve an immutable proposal only for broker sandbox automation.

        This is deliberately not an owner-decision surrogate. Production rows
        can never use this transition, and ordinary claim_approved calls still
        require the signed Telegram owner decision.
        """
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            row = self._expire_row(c, row)
            terms = row.get("terms_json") or {}
            if terms.get("execution_environment") != "sandbox":
                raise ApprovalError("SANDBOX_AUTOTRADE_ONLY", 403)
            if row["status"] == "APPROVED" and row.get("reason_code") == "SANDBOX_AUTO_APPROVED":
                return self._view(row, idempotent=True, auto_approved=True)
            if row["status"] not in {"PENDING_DELIVERY", "AWAITING_OWNER"}:
                return self._view(row, accepted=False, auto_approved=False)
            if self._has_active(c, row):
                return self._view(row, accepted=False,
                                  reason_code="INSTRUMENT_HAS_UNSETTLED_EXECUTION")
            now = self._now()
            row = self._change(c, row, {
                "status": "APPROVED", "approved_at": now, "approved_by": None,
                "reason_code": "SANDBOX_AUTO_APPROVED",
            }, "SANDBOX_AUTO_APPROVED", {"owner_confirmation": False})
            return self._view(row, accepted=True, auto_approved=True)

    def block(self, proposal_id, reason_code):
        reason_code = _reason(reason_code)
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            row = self._expire_row(c, row)
            if row["status"] in {"BLOCKED", "EXPIRED", "REJECTED"}:
                return self._view(row, idempotent=True)
            if row["status"] not in PRE_SUBMISSION:
                raise ApprovalError("SUBMISSION_CANNOT_BE_REQUEUED_OR_BLOCKED")
            row = self._change(c, row, {"status": "BLOCKED", "reason_code": reason_code},
                               "BLOCKED")
            return self._view(row)

    def _require_owner_approval(self, c, row, *, allow_sandbox_auto=False):
        if type(allow_sandbox_auto) is not bool:
            raise ApprovalError("BOOLEAN_SANDBOX_AUTO_GATE_REQUIRED", 400)
        terms = row.get("terms_json") or {}
        if allow_sandbox_auto:
            if (terms.get("execution_environment") == "sandbox"
                    and row.get("reason_code") == "SANDBOX_AUTO_APPROVED"
                    and row.get("approved_at") is not None
                    and row.get("approved_by") is None
                    and _date(row["approved_at"]) < _date(row["expires_at"])):
                return
            if terms.get("execution_environment") != "sandbox":
                raise ApprovalError("SANDBOX_AUTOTRADE_ONLY", 403)
        proof = c.execute(f"SELECT * FROM {CALLBACKS} WHERE proposal_id=%s",
                          (row["proposal_id"],)).fetchone()
        if proof is None:
            raise ApprovalError("SIGNED_OWNER_APPROVAL_REQUIRED", 403)
        proof = self._verify_decision(dict(proof))
        valid = (
            proof["action"] == "a" and proof["decision_status"] == "APPROVED"
            and proof["terms_hash"] == row["terms_hash"]
            and proof["sender_user_id"] == row["owner_user_id"] == row["approved_by"]
            and proof["private_chat_id"] == row["private_chat_id"]
            and proof["bot_id"] == row["bot_id"]
            and proof["message_id"] == row["telegram_message_id"]
            and row["approved_at"] is not None
            and _date(proof["decided_at"]) == _date(row["approved_at"])
            and _date(proof["decided_at"]) < _date(row["expires_at"])
        )
        if not valid:
            raise ApprovalError("SIGNED_OWNER_APPROVAL_REQUIRED", 403)

    def claim_approved(self, proposal_id, *, terms_hash, worker_id, economics_revision=1,
                       allow_sandbox_auto=False):
        """Commit SENDING before returning; a lost claim response is not retried."""
        if type(allow_sandbox_auto) is not bool:
            raise ApprovalError("BOOLEAN_SANDBOX_AUTO_GATE_REQUIRED", 400)
        worker_id = _text(worker_id, "WORKER_ID", 128)
        with self._transaction() as c:
            row = self._locked(c, proposal_id, skip_locked=True)
            if row is None:
                return None
            if (not isinstance(terms_hash, str)
                    or not hmac.compare_digest(terms_hash, row["terms_hash"])
                    or type(economics_revision) is not int
                    or economics_revision != row["economics_revision"]):
                raise ApprovalError("APPROVED_ECONOMICS_MISMATCH")
            row = self._expire_row(c, row)
            if row["status"] != "APPROVED":
                return None
            self._require_owner_approval(c, row, allow_sandbox_auto=allow_sandbox_auto)
            if self._has_active(c, row):
                raise ApprovalError("INSTRUMENT_HAS_UNSETTLED_EXECUTION")
            now = self._now()
            row = self._change(c, row, {
                "status": "SENDING", "claim_token": secrets.token_urlsafe(24),
                "worker_id": worker_id, "send_started_at": now,
                "send_deadline": now + timedelta(seconds=SEND_TIMEOUT_SECONDS),
                "reason_code": None,
            }, "SUBMISSION_CLAIMED")
            return self._view(row)

    def abort_unsubmitted_claim(self, proposal_id, claim_token, reason_code):
        """Finish a claimed order only when its coordinator never called the adapter.

        This internal transition must not be used after any submission attempt,
        transport ambiguity or broker acknowledgement. It does not manufacture a
        broker rejection or a fill. The immutable request UUID remains consumed.
        """
        reason_code = _reason(reason_code)
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            if (not isinstance(claim_token, str) or not row["claim_token"]
                    or not hmac.compare_digest(claim_token, row["claim_token"])):
                raise ApprovalError("CLAIM_TOKEN_MISMATCH", 403)
            if (row["status"] == "BLOCKED" and row["reason_code"] == reason_code
                    and row["execution_reconciled"] and row["broker_order_id"] is None
                    and row["filled_lots"] == 0):
                return self._view(row, idempotent=True, aborted_without_submission=True)
            if (row["status"] != "SENDING" or row["broker_order_id"] is not None
                    or row["filled_lots"] not in (None, 0) or row["execution_reconciled"]):
                raise ApprovalError("SUBMITTED_EXECUTION_CANNOT_BE_ABORTED")
            row = self._change(c, row, {"status": "BLOCKED", "reason_code": reason_code,
                "filled_lots": 0, "execution_reconciled": True}, "SUBMISSION_ABORTED_BEFORE_BROKER_IO",
                {"broker_io_attempted": False, "reason_code": reason_code})
            return self._view(row, aborted_without_submission=True)

    def _broker_state(self, value):
        aliases = {
            "NEW": "ACKNOWLEDGED", "ACKNOWLEDGED": "ACKNOWLEDGED",
            "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
            "CANCELLED": "CANCELLED", "REJECTED": "BROKER_REJECTED",
            "BROKER_REJECTED": "BROKER_REJECTED", "UNKNOWN": "UNKNOWN",
            "EXECUTION_REPORT_STATUS_NEW": "ACKNOWLEDGED",
            "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PARTIALLY_FILLED",
            "EXECUTION_REPORT_STATUS_FILL": "FILLED",
            "EXECUTION_REPORT_STATUS_CANCELLED": "CANCELLED",
            "EXECUTION_REPORT_STATUS_REJECTED": "BROKER_REJECTED",
        }
        if not isinstance(value, str) or value not in aliases:
            raise ApprovalError("INVALID_BROKER_STATUS", 400)
        return aliases[value]

    def _observe(self, c, row, *, broker_order_id, broker_status, filled_lots,
                 client_order_id=None, observed_at=None, average_fill_price=None,
                 require_request_binding=False):
        if row["status"] not in UNSETTLED_EXECUTION | TERMINAL_EXECUTION:
            raise ApprovalError("SUBMISSION_NOT_STARTED")
        if client_order_id is not None and client_order_id != row["client_order_id"]:
            raise ApprovalError("CLIENT_ORDER_ID_MISMATCH", 403)
        state = self._broker_state(broker_status)
        if broker_order_id is not None:
            broker_order_id = _text(broker_order_id, "BROKER_ORDER_ID", 256)
        if row["broker_order_id"] is not None:
            if broker_order_id is None and state == "UNKNOWN":
                broker_order_id = row["broker_order_id"]
            if broker_order_id != row["broker_order_id"]:
                raise ApprovalError("BROKER_ORDER_ID_MISMATCH", 403)
        elif broker_order_id is not None and require_request_binding:
            if client_order_id != row["client_order_id"]:
                raise ApprovalError("CLIENT_ORDER_ID_REQUIRED_FOR_FIRST_BIND", 403)
        if filled_lots is not None and (
            type(filled_lots) is not int or not 0 <= filled_lots <= row["lots"]
        ):
            raise ApprovalError("INVALID_EXECUTED_LOTS", 400)
        if state != "UNKNOWN" and filled_lots is None:
            raise ApprovalError("KNOWN_EXECUTED_LOTS_REQUIRED", 400)
        if state == "UNKNOWN" and filled_lots is not None:
            raise ApprovalError("UNKNOWN_FILL_COUNT_MUST_BE_NULL", 400)
        if (state == "FILLED" and filled_lots != row["lots"]) or (
            state == "PARTIALLY_FILLED" and not 0 < filled_lots < row["lots"]
        ) or (state == "ACKNOWLEDGED" and filled_lots != 0):
            raise ApprovalError("BROKER_STATUS_FILL_MISMATCH", 400)
        if state not in {"UNKNOWN", "BROKER_REJECTED"} and broker_order_id is None:
            raise ApprovalError("BROKER_ORDER_ID_REQUIRED", 400)
        if state == "BROKER_REJECTED" and filled_lots and broker_order_id is None:
            raise ApprovalError("BROKER_ORDER_ID_REQUIRED", 400)
        now = self._now()
        observed = _date(observed_at) if observed_at is not None else now
        if observed > now + timedelta(seconds=5):
            raise ApprovalError("FUTURE_BROKER_OBSERVATION", 400)
        average = None
        if average_fill_price is not None:
            average = _decimal_string(_decimal(average_fill_price, positive=True))
            if filled_lots is None or filled_lots <= 0:
                raise ApprovalError("FILL_PRICE_WITHOUT_EXECUTION", 400)
        if row["broker_observed_at"] is not None and observed < _date(row["broker_observed_at"]):
            return self._view(row, idempotent=True, ignored=True)
        if (row["filled_lots"] is not None and filled_lots is not None
                and filled_lots < row["filled_lots"]):
            return self._view(row, idempotent=True, ignored=True)
        old_state = row["status"]
        # An ambiguous later transport observation cannot erase a known broker
        # receipt or any actual execution. UNKNOWN is not a fresh submission.
        if state == "UNKNOWN" and old_state not in {"SENDING", "UNKNOWN"}:
            return self._view(row, idempotent=True, ignored=True)
        if old_state in TERMINAL_EXECUTION:
            allowed = state == old_state or (
                old_state in {"CANCELLED", "BROKER_REJECTED"} and state == "FILLED"
            )
            if not allowed:
                return self._view(row, idempotent=True, ignored=True)
        if (old_state == "PARTIALLY_FILLED" and state == "ACKNOWLEDGED"):
            return self._view(row, idempotent=True, ignored=True)
        if average is None and filled_lots == row["filled_lots"]:
            average = row["average_fill_price"]
        values = {
            "status": state, "broker_order_id": broker_order_id,
            "broker_status": broker_status, "filled_lots": filled_lots,
            "average_fill_price": average,
        }
        changed = any(row[k] != v for k, v in values.items())
        if not changed:
            if row["broker_observed_at"] is None or observed > _date(row["broker_observed_at"]):
                row = self._change(c, row, {"broker_observed_at": observed}, "BROKER_OBSERVED")
            return self._view(row, idempotent=True)
        values.update(
            broker_observed_at=observed, execution_reconciled=False,
            reason_code="BROKER_OUTCOME_UNKNOWN" if state == "UNKNOWN" else None,
        )
        row = self._change(c, row, values, "BROKER_OBSERVED")
        return self._view(row)

    def record_submission(self, proposal_id, claim_token, *, outcome,
                          broker_order_id=None, broker_status=None, filled_lots=None,
                          client_order_id=None, observed_at=None, average_fill_price=None):
        """Record a receipt for the one claimed call; never request another call."""
        if outcome not in {"ACCEPTED", "REJECTED", "UNKNOWN"}:
            raise ApprovalError("INVALID_SUBMISSION_OUTCOME", 400)
        if outcome == "UNKNOWN":
            status = "UNKNOWN"
        elif outcome == "REJECTED":
            status = broker_status or "BROKER_REJECTED"
            if self._broker_state(status) != "BROKER_REJECTED":
                raise ApprovalError("SUBMISSION_OUTCOME_STATUS_MISMATCH", 400)
            if filled_lots is None:
                filled_lots = 0
        else:
            status = broker_status or "NEW"
            if self._broker_state(status) in {"UNKNOWN", "BROKER_REJECTED"}:
                raise ApprovalError("SUBMISSION_OUTCOME_STATUS_MISMATCH", 400)
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            if (not isinstance(claim_token, str) or not row["claim_token"]
                    or not hmac.compare_digest(claim_token, row["claim_token"])):
                raise ApprovalError("CLAIM_TOKEN_MISMATCH", 403)
            return self._observe(
                c, row, broker_order_id=broker_order_id, broker_status=status,
                filled_lots=filled_lots, client_order_id=client_order_id,
                observed_at=observed_at, average_fill_price=average_fill_price,
            )

    def update_execution(self, proposal_id, *, broker_order_id, broker_status, filled_lots,
                         client_order_id=None, observed_at=None, average_fill_price=None,
                         execution_id=None):
        """Apply a verified cumulative broker state, not a synthetic fill ledger.

        A previously unknown broker order is bound only by the exact original
        client request UUID. The transport must also verify instrument/side/lots.
        Persist actual trade stages and known commissions in the ledger before
        calling mark_execution_reconciled for a terminal result.
        """
        if execution_id is not None:
            _text(execution_id, "EXECUTION_ID", 256)
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            return self._observe(
                c, row, broker_order_id=broker_order_id, broker_status=broker_status,
                filled_lots=filled_lots, client_order_id=client_order_id,
                observed_at=observed_at, average_fill_price=average_fill_price,
                require_request_binding=True,
            )

    def mark_execution_reconciled(self, proposal_id, broker_order_id, filled_lots):
        """Acknowledge already committed actual fills and complete known fees.

        A definitive broker rejection without an order ID and with zero fills
        may be acknowledged directly. UNKNOWN can never release the scope.
        """
        with self._transaction() as c:
            row = self._locked(c, proposal_id)
            if row["status"] not in TERMINAL_EXECUTION:
                raise ApprovalError("TERMINAL_EXECUTION_REQUIRED")
            if (broker_order_id != row["broker_order_id"]
                    or type(filled_lots) is not int or filled_lots != row["filled_lots"]):
                raise ApprovalError("EXECUTION_RECONCILIATION_MISMATCH")
            if broker_order_id is None and not (
                row["status"] == "BROKER_REJECTED" and filled_lots == 0
            ):
                raise ApprovalError("BROKER_ORDER_ID_REQUIRED", 400)
            if row["execution_reconciled"]:
                return self._view(row, idempotent=True)
            row = self._change(c, row, {"execution_reconciled": True},
                               "EXECUTION_RECONCILED")
            return self._view(row)

    def expire(self):
        """Expire entry proposals and retire uncertain sends without requeuing."""
        now = self._now()
        with self.connect() as c:
            rows = c.execute(f"""SELECT proposal_id FROM {TABLE}
                WHERE (status IN ('PENDING_DELIVERY','DELIVERY_SENDING','DELIVERY_UNKNOWN',
                                  'AWAITING_OWNER','APPROVED') AND expires_at <= %s)
                   OR (status='DELIVERY_SENDING' AND delivery_deadline <= %s)
                   OR (status='SENDING' AND send_deadline <= %s)
                ORDER BY created_at, proposal_id LIMIT 1000""", (now, now, now)).fetchall()
        changes = []
        for item in rows:
            with self._transaction() as c:
                row = self._locked(c, item["proposal_id"], skip_locked=True)
                if row is None:
                    continue
                before = row["status"]
                row = self._expire_row(c, row)
                if row["status"] != before:
                    changes.append({"proposal_id": row["proposal_id"], "status": row["status"]})
        return changes

    def list_by_status(self, statuses, *, account_id=None, owner_user_id=None,
                       limit=100, include_unreconciled_terminal=False):
        statuses = [statuses] if isinstance(statuses, str) else list(statuses)
        if any(s not in STATUSES for s in statuses):
            raise ApprovalError("INVALID_PROPOSAL_STATUS", 400)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ApprovalError("INVALID_LIST_LIMIT", 400)
        params = []
        parts = []
        if statuses:
            parts.append("status IN (" + ",".join("%s" for _ in statuses) + ")")
            params.extend(statuses)
        if include_unreconciled_terminal:
            parts.append("(status IN ('FILLED','CANCELLED','BROKER_REJECTED') "
                         "AND execution_reconciled=FALSE)")
        if not parts:
            return []
        where = ["(" + " OR ".join(parts) + ")"]
        if account_id is not None:
            where.append("account_id=%s")
            params.append(_text(account_id, "ACCOUNT_ID"))
        if owner_user_id is not None:
            where.append("owner_user_id=%s")
            params.append(_positive_id(owner_user_id))
        params.append(limit)
        with self.connect() as c:
            rows = c.execute(f"SELECT * FROM {TABLE} WHERE " + " AND ".join(where)
                             + " ORDER BY created_at,proposal_id LIMIT %s",
                             tuple(params)).fetchall()
            return [self._view(row) for row in rows]

    def list_pending(self, **filters):
        return self.list_by_status(("PENDING_DELIVERY", "AWAITING_OWNER"), **filters)

    def list_approved(self, **filters):
        return self.list_by_status(("APPROVED",), **filters)

    def list_unsettled(self, **filters):
        return self.list_by_status(sorted(UNSETTLED_EXECUTION),
                                   include_unreconciled_terminal=True, **filters)

    def list_recent(self, *, account_id=None, owner_user_id=None, limit=20,
                    updated_after=None, instrument_uid=None, execution_environment=None):
        """Newest records for status display; filter the scope before LIMIT."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ApprovalError("INVALID_LIST_LIMIT", 400)
        where, params = [], []
        for column, value in (("account_id", account_id), ("instrument_uid", instrument_uid)):
            if value is not None:
                where.append(column + "=%s")
                params.append(_text(value, column.upper()))
        if owner_user_id is not None:
            where.append("owner_user_id=%s")
            params.append(_positive_id(owner_user_id))
        if updated_after is not None:
            where.append("updated_at>%s")
            params.append(_date(updated_after))
        if execution_environment is not None:
            if execution_environment not in {"sandbox", "production"}:
                raise ApprovalError("INVALID_EXECUTION_ENVIRONMENT", 400)
            where.append("terms_json->>'execution_environment'=%s")
            params.append(execution_environment)
        query = f"SELECT * FROM {TABLE}"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY updated_at DESC,proposal_id DESC LIMIT %s"
        params.append(limit)
        with self.connect() as c:
            return [self._view(row) for row in c.execute(query, tuple(params)).fetchall()]
