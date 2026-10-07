"""Private operator console for the existing Currency execution service.

An expiring, single-use setup grant creates a cookie session. Account selection
and a two-channel Telegram pairing are explicit operator actions. No browser
operation approves a financial proposal. Secrets never enter an HTML response.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
import hashlib
import hmac
import json
import os
import re
import secrets
import threading

ORIGIN = "https://veritas-intelligence-v1.onrender.com"
PAGE = "/integrations/trading"
API = "/api/v1/currency-trading/"
INTERNAL = "/internal/currency-console/"
COOKIE = "__Host-veritas-trade"
EXPECTED_BOT = "axednewsi_bot"
SESSIONS = "public.veritas_trade_console_sessions"
GRANTS = "public.veritas_trade_console_grants"
CONFIG = "public.veritas_trade_console_config"
PAIRINGS = "public.veritas_trade_console_pairings"
AUDIT = "public.veritas_trade_console_audit"
LOGINS = "public.veritas_trade_console_logins"
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,96}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class ConsoleError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def utcnow():
    return datetime.now(timezone.utc)


def enabled(name):
    return os.getenv(name, "").lower() in ("1", "true", "yes", "on")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ConsoleError("INVALID_ACCESS_CODE", 401)
    return value


def positive_id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ConsoleError("INVALID_TELEGRAM_ID", 400)
    return value


def clean_text(value, maximum=160):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 for c in value)):
        raise ConsoleError("INVALID_IDENTIFIER", 400)
    return value


def environment():
    value = os.getenv("VERITAS_CURRENCY_TRADE_ENVIRONMENT", "production")
    if value not in ("production", "sandbox"):
        raise ConsoleError("INVALID_EXECUTION_ENVIRONMENT", 503)
    return value


def session_cookie(value, max_age=43200):
    return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"


def request_token(headers):
    try:
        jar = SimpleCookie()
        jar.load(headers.get("Cookie", ""))
        return token(jar[COOKIE].value)
    except (KeyError, ValueError):
        raise ConsoleError("AUTHENTICATION_REQUIRED", 401) from None


class ConsoleStore:
    def __init__(self, connect, clock=utcnow):
        self.connect, self.clock = connect, clock
        self._ready = False
        self._lock = threading.RLock()

    def _run(self, callback):
        with self.connect() as c:
            with c.transaction():
                c.execute("SET LOCAL statement_timeout='2500ms'")
                c.execute("SET LOCAL lock_timeout='1500ms'")
                return callback(c)

    def ensure_schema(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            def create(c):
                c.execute(f"""CREATE TABLE IF NOT EXISTS {GRANTS} (
                  grant_hash TEXT PRIMARY KEY, consumed_at TIMESTAMPTZ NOT NULL)""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS {SESSIONS} (
                  session_hash TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL, revoked BOOLEAN NOT NULL DEFAULT FALSE,
                  environment TEXT NOT NULL, owner_user_id BIGINT, bot_id BIGINT)""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS {CONFIG} (
                  environment TEXT PRIMARY KEY, account_id TEXT,
                  owner_user_id BIGINT, owner_chat_id BIGINT, bot_id BIGINT,
                  owner_name TEXT, paused BOOLEAN NOT NULL DEFAULT TRUE,
                  execution_requested BOOLEAN NOT NULL DEFAULT FALSE,
                  revision BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL,
                  CHECK (environment IN ('production','sandbox')),
                  CHECK (owner_user_id IS NULL OR owner_user_id=owner_chat_id))""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS {PAIRINGS} (
                  challenge_id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
                  session_hash TEXT NOT NULL, environment TEXT NOT NULL,
                  expires_at TIMESTAMPTZ NOT NULL, candidate_user_id BIGINT,
                  private_chat_id BIGINT, bot_id BIGINT, user_name TEXT,
                  observed_at TIMESTAMPTZ, confirmed_at TIMESTAMPTZ)""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS {AUDIT} (
                  id BIGSERIAL PRIMARY KEY, environment TEXT NOT NULL,
                  action TEXT NOT NULL, actor_hash TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL, details JSONB NOT NULL)""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS {LOGINS} (
                  code_hash TEXT PRIMARY KEY, environment TEXT NOT NULL,
                  owner_user_id BIGINT NOT NULL, bot_id BIGINT NOT NULL,
                  expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ)""")
            self._run(create)
            self._ready = True

    def _audit(self, c, action, actor, details):
        c.execute(f"INSERT INTO {AUDIT} (environment,action,actor_hash,created_at,details) "
                  "VALUES (%s,%s,%s,%s,%s::jsonb)",
                  (environment(), action, actor, self.clock(), json.dumps(details)))

    def open_session(self, setup_code, expected_hash, expires_at):
        code = token(setup_code)
        now = self.clock()
        bootstrap = (isinstance(expected_hash, str) and _DIGEST.fullmatch(expected_hash)
                     and hmac.compare_digest(digest(code), expected_hash))
        if bootstrap and (not isinstance(expires_at, datetime) or expires_at.tzinfo is None or now >= expires_at):
            raise ConsoleError("ACCESS_CODE_EXPIRED", 401)
        self.ensure_schema()
        session = secrets.token_urlsafe(32)
        def consume(c):
            session_owner, session_bot = None, None
            if bootstrap:
                used = c.execute(f"INSERT INTO {GRANTS} VALUES (%s,%s) ON CONFLICT DO NOTHING "
                                 "RETURNING grant_hash", (expected_hash, now)).fetchone()
                if not used:
                    raise ConsoleError("ACCESS_CODE_ALREADY_USED", 401)
            else:
                row = c.execute(f"SELECT l.* FROM {LOGINS} l JOIN {CONFIG} b USING (environment) "
                    "WHERE l.code_hash=%s AND l.environment=%s AND l.owner_user_id=b.owner_user_id "
                    "AND l.bot_id=b.bot_id AND l.expires_at>%s AND l.consumed_at IS NULL FOR UPDATE OF l",
                    (digest(code), environment(), now)).fetchone()
                if not row:
                    raise ConsoleError("INVALID_OR_EXPIRED_LOGIN_LINK", 401)
                session_owner, session_bot = row["owner_user_id"], row["bot_id"]
                c.execute(f"UPDATE {LOGINS} SET consumed_at=%s WHERE code_hash=%s", (now, digest(code)))
            c.execute(f"INSERT INTO {SESSIONS} (session_hash,expires_at,created_at,environment,owner_user_id,bot_id) "
                      "VALUES (%s,%s,%s,%s,%s,%s)",
                      (digest(session), now + timedelta(hours=12), now, environment(), session_owner, session_bot))
            self._audit(c, "SESSION_CREATED", digest(session), {})
        self._run(consume)
        return session

    def owner_login(self, payload):
        uid, chat, bid = (positive_id(payload.get(k)) for k in ("user_id", "private_chat_id", "bot_id"))
        if (uid != chat or payload.get("chat_type") != "private" or payload.get("is_bot") is not False
                or str(payload.get("bot_username", "")).lower() != EXPECTED_BOT):
            raise ConsoleError("PRIVATE_OWNER_PROOF_REQUIRED", 403)
        self.ensure_schema()
        code, now = secrets.token_urlsafe(32), self.clock()
        expires = now + timedelta(minutes=10)
        def create(c):
            row = c.execute(f"SELECT * FROM {CONFIG} WHERE environment=%s FOR UPDATE", (environment(),)).fetchone()
            if not row or (row["owner_user_id"], row["owner_chat_id"], row["bot_id"]) != (uid, chat, bid):
                raise ConsoleError("BOUND_PRIVATE_OWNER_REQUIRED", 403)
            c.execute(f"UPDATE {LOGINS} SET expires_at=%s WHERE environment=%s AND owner_user_id=%s "
                      "AND consumed_at IS NULL", (now, environment(), uid))
            c.execute(f"INSERT INTO {LOGINS} (code_hash,environment,owner_user_id,bot_id,expires_at) "
                      "VALUES (%s,%s,%s,%s,%s)", (digest(code), environment(), uid, bid, expires))
            self._audit(c, "OWNER_LOGIN_LINK_CREATED", "telegram:" + str(uid), {})
        self._run(create)
        return {"ok": True, "login_url": ORIGIN + PAGE + "#setup=" + code,
                "expires_at": expires.isoformat()}

    def authenticate(self, session):
        key = digest(token(session))
        self.ensure_schema()
        row = self._run(lambda c: c.execute(
            f"SELECT s.session_hash FROM {SESSIONS} s LEFT JOIN {CONFIG} b USING (environment) "
            "WHERE s.session_hash=%s AND s.expires_at>%s AND s.environment=%s AND NOT s.revoked "
            "AND (s.owner_user_id IS NULL OR (s.owner_user_id=b.owner_user_id AND s.bot_id=b.bot_id))",
            (key, self.clock(), environment())).fetchone())
        if not row:
            raise ConsoleError("SESSION_EXPIRED", 401)
        return key

    def binding(self):
        self.ensure_schema()
        row = self._run(lambda c: c.execute(
            f"SELECT * FROM {CONFIG} WHERE environment=%s", (environment(),)).fetchone())
        return dict(row) if row else {"environment": environment(), "paused": True,
                                    "execution_requested": False, "revision": 0}

    def select_account(self, account_id, actor):
        account = clean_text(account_id)
        def select(c):
            c.execute(f"INSERT INTO {CONFIG} (environment,updated_at) VALUES (%s,%s) "
                      "ON CONFLICT DO NOTHING", (environment(), self.clock()))
            old = c.execute(f"SELECT * FROM {CONFIG} WHERE environment=%s FOR UPDATE",
                            (environment(),)).fetchone()
            if old["account_id"] not in (None, account):
                raise ConsoleError("ACCOUNT_ALREADY_BOUND")
            c.execute(f"UPDATE {CONFIG} SET account_id=%s,revision=revision+1,updated_at=%s "
                      "WHERE environment=%s", (account, self.clock(), environment()))
            self._audit(c, "ACCOUNT_SELECTED", actor, {"account_id": account})
        self.ensure_schema()
        self._run(select)

    def new_pairing(self, actor):
        self.ensure_schema()
        code, challenge = secrets.token_urlsafe(32), secrets.token_hex(16)
        until = self.clock() + timedelta(minutes=10)
        def insert(c):
            c.execute(f"UPDATE {PAIRINGS} SET expires_at=%s WHERE session_hash=%s "
                      "AND confirmed_at IS NULL", (self.clock(), actor))
            c.execute(f"INSERT INTO {PAIRINGS} (challenge_id,code_hash,session_hash,environment,expires_at) "
                      "VALUES (%s,%s,%s,%s,%s)", (challenge, digest(code), actor, environment(), until))
            self._audit(c, "OWNER_PAIRING_CREATED", actor, {"challenge_id": challenge})
        self._run(insert)
        return {"pairing_url": "https://t.me/AxednewsI_bot?start=vt_" + code,
                "pairing_expires_at": until.isoformat(), "challenge_id": challenge}

    def observe_owner(self, payload):
        code = token(payload.get("pairing_code"))
        uid, chat, bid = (positive_id(payload.get(k)) for k in
                          ("user_id", "private_chat_id", "bot_id"))
        if (uid != chat or payload.get("chat_type") != "private" or payload.get("is_bot") is not False
                or str(payload.get("bot_username", "")).lower() != EXPECTED_BOT):
            raise ConsoleError("PRIVATE_OWNER_PROOF_REQUIRED", 403)
        name = str(payload.get("user_name") or "")[:160]
        self.ensure_schema()
        def observe(c):
            row = c.execute(f"SELECT * FROM {PAIRINGS} WHERE code_hash=%s AND environment=%s FOR UPDATE",
                            (digest(code), environment())).fetchone()
            if not row or row["expires_at"] <= self.clock() or row["confirmed_at"]:
                raise ConsoleError("PAIRING_EXPIRED", 410)
            if row["candidate_user_id"] is not None and (row["candidate_user_id"], row["bot_id"]) != (uid, bid):
                raise ConsoleError("PAIRING_ALREADY_CLAIMED", 403)
            c.execute(f"UPDATE {PAIRINGS} SET candidate_user_id=%s,private_chat_id=%s,bot_id=%s,"
                      "user_name=%s,observed_at=%s WHERE challenge_id=%s",
                      (uid, chat, bid, name, self.clock(), row["challenge_id"]))
            self._audit(c, "OWNER_PAIRING_OBSERVED", "telegram:" + str(uid),
                        {"challenge_id": row["challenge_id"], "bot_id": bid})
            return {"ok": True, "status": "CONFIRM_IN_PRIVATE_CONSOLE"}
        return self._run(observe)

    def pending_owner(self, actor):
        self.ensure_schema()
        row = self._run(lambda c: c.execute(
            f"SELECT challenge_id,candidate_user_id,private_chat_id,user_name FROM {PAIRINGS} "
            "WHERE session_hash=%s AND environment=%s AND confirmed_at IS NULL AND expires_at>%s "
            "AND candidate_user_id IS NOT NULL ORDER BY observed_at DESC LIMIT 1",
            (actor, environment(), self.clock())).fetchone())
        return ({"challenge_id": row["challenge_id"], "user_id": row["candidate_user_id"],
                 "private_chat_id": row["private_chat_id"], "user_name": row["user_name"]} if row else None)

    def confirm_owner(self, challenge_id, actor):
        challenge = clean_text(challenge_id, 64)
        def confirm(c):
            row = c.execute(f"SELECT * FROM {PAIRINGS} WHERE challenge_id=%s FOR UPDATE", (challenge,)).fetchone()
            if (not row or row["session_hash"] != actor or row["environment"] != environment()
                    or row["expires_at"] <= self.clock() or row["confirmed_at"]
                    or not row["candidate_user_id"]):
                raise ConsoleError("PAIRING_NOT_READY", 409)
            c.execute(f"INSERT INTO {CONFIG} (environment,updated_at) VALUES (%s,%s) "
                      "ON CONFLICT DO NOTHING", (environment(), self.clock()))
            old = c.execute(f"SELECT * FROM {CONFIG} WHERE environment=%s FOR UPDATE", (environment(),)).fetchone()
            if old["owner_user_id"] is not None and (old["owner_user_id"], old["bot_id"]) != (
                    row["candidate_user_id"], row["bot_id"]):
                raise ConsoleError("OWNER_ALREADY_BOUND", 403)
            c.execute(f"UPDATE {CONFIG} SET owner_user_id=%s,owner_chat_id=%s,bot_id=%s,owner_name=%s,"
                      "paused=TRUE,execution_requested=FALSE,revision=revision+1,updated_at=%s WHERE environment=%s",
                      (row["candidate_user_id"], row["private_chat_id"], row["bot_id"], row["user_name"],
                       self.clock(), environment()))
            c.execute(f"UPDATE {PAIRINGS} SET confirmed_at=%s WHERE challenge_id=%s", (self.clock(), challenge))
            c.execute(f"UPDATE {SESSIONS} SET owner_user_id=%s,bot_id=%s WHERE session_hash=%s AND environment=%s",
                      (row["candidate_user_id"], row["bot_id"], actor, environment()))
            self._audit(c, "OWNER_CONFIRMED", actor, {"user_id": row["candidate_user_id"], "bot_id": row["bot_id"]})
        self._run(confirm)

    def set_operation(self, action, actor):
        if action not in ("pause", "resume", "enable_execution", "disable_execution"):
            raise ConsoleError("INVALID_CONSOLE_ACTION", 400)
        def change(c):
            row = c.execute(f"SELECT * FROM {CONFIG} WHERE environment=%s FOR UPDATE", (environment(),)).fetchone()
            if not row or not row["account_id"] or not row["owner_user_id"]:
                raise ConsoleError("ACCOUNT_AND_OWNER_BINDING_REQUIRED")
            paused = action == "pause" or (row["paused"] and action != "resume")
            armed = action == "enable_execution" or (row["execution_requested"] and action not in ("pause", "disable_execution"))
            c.execute(f"UPDATE {CONFIG} SET paused=%s,execution_requested=%s,revision=revision+1,updated_at=%s "
                      "WHERE environment=%s", (paused, armed, self.clock(), environment()))
            self._audit(c, action.upper(), actor, {})
        self._run(change)

    @contextmanager
    def execution_guard(self, account_id, owner_user_id, private_chat_id, bot_id):
        """Serialize permission changes with the final, exactly-once send boundary."""
        from veritas_currency_trade_plan import TradePlanBlocked
        self.ensure_schema()
        with self.connect() as c:
            with c.transaction():
                c.execute("SET LOCAL statement_timeout='2500ms'")
                c.execute("SET LOCAL lock_timeout='1500ms'")
                row = c.execute(f"SELECT * FROM {CONFIG} WHERE environment=%s FOR UPDATE", (environment(),)).fetchone()
                if (not row or (row["account_id"], row["owner_user_id"], row["owner_chat_id"], row["bot_id"]) !=
                        (account_id, owner_user_id, private_chat_id, bot_id) or row["paused"] is not False
                        or row["execution_requested"] is not True):
                    raise TradePlanBlocked("EXECUTION_PAUSED_OR_SCOPE_CHANGED")
                yield True


_STORES = {}
_LOCK = threading.RLock()
_SUMMARY_PROVIDERS = {}


def summary_provider(state_lock, state):
    """Cache one callable over the shared, in-place research snapshot."""
    key = (id(state_lock), id(state))
    with _LOCK:
        if key not in _SUMMARY_PROVIDERS:
            def snapshot():
                with state_lock:
                    return deepcopy(state.get("summary") or [])
            _SUMMARY_PROVIDERS[key] = snapshot
        return _SUMMARY_PROVIDERS[key]


def store_for(connect):
    with _LOCK:
        if connect not in _STORES:
            _STORES[connect] = ConsoleStore(connect)
        return _STORES[connect]


def load_binding(connect):
    """Only an enabled console may supply explicitly persisted owner bindings."""
    if not enabled("VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED"):
        return {}
    return store_for(connect).binding()


def _csrf(session):
    key = os.getenv("VERITAS_CURRENCY_TRADE_SESSION_KEY", "")
    if len(key.encode()) < 32:
        raise ConsoleError("CONSOLE_SESSION_KEY_REQUIRED", 503)
    return hmac.new(key.encode(), session.encode(), hashlib.sha256).hexdigest()


def _adapter(account=None):
    from veritas_tbank_trading import TBankTradingAdapter, ExecutionConfig
    from veritas_currency_trade_service import CNY_UID
    token_name = "TBANK_API_TOKEN" if environment() == "production" else "TBANK_SANDBOX_TOKEN"
    secret = os.getenv(token_name, "").strip()
    if not secret:
        raise ConsoleError("BROKER_TOKEN_NOT_CONFIGURED", 503)
    return TBankTradingAdapter(secret, config=ExecutionConfig(
        allowed_account_ids=frozenset({account}) if account else frozenset(),
        allowed_instrument_uids=frozenset({CNY_UID}), environment=environment()))


def _accounts():
    rows = _adapter().get_accounts()
    if not isinstance(rows, list):
        raise ConsoleError("BROKER_ACCOUNTS_UNAVAILABLE", 503)
    return [{"account_id": clean_text(r.get("id")), "name": str(r.get("name") or "Брокерский счёт"),
             "status": r.get("status"), "eligible": r.get("status") == "ACCOUNT_STATUS_OPEN"
             and r.get("accessLevel") == "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"}
            for r in rows if isinstance(r, Mapping) and r.get("id")]


def _application(connect, summary):
    import veritas_currency_trade_service as service
    app = service.get_application(connect, summary)
    if app is None:
        raise ConsoleError("CURRENCY_TRADE_PROPOSALS_DISABLED", 503)
    return app


def _bind_ledger(connect, summary):
    app = _application(connect, summary)
    with app._lock:
        app._initialize()
        if not app.facts.is_bound():
            app.facts.bind()


def _dashboard(connect, summary, actor):
    repository = store_for(connect)
    binding = repository.binding()
    pending = repository.pending_owner(actor)
    result = {"authenticated": True, "updated_at": utcnow().isoformat(), "mode": environment(),
              "paused": binding.get("paused", True), "execution_requested": binding.get("execution_requested", False),
              "connection": {"status": "NOT_BOUND", "account_id": binding.get("account_id")},
              "owner": {"status": "BOUND" if binding.get("owner_user_id") else "NOT_BOUND",
                        "user_id": binding.get("owner_user_id"), "private_chat_id": binding.get("owner_chat_id"),
                        "user_name": binding.get("owner_name"), "bot_username": "AxednewsI_bot"},
              "accounts": [], "allocation": {"allocation_rub": "10000", "max_leverage": 10},
              "readiness": {"status": "BLOCKED", "blockers": []}, "positions": [], "orders": [], "fills": [],
              "costs": {}, "actions": {}, "setup": {"required": not(binding.get("account_id") and binding.get("owner_user_id")),
                  "can_select_account": not bool(binding.get("account_id")), "can_pair_owner": not binding.get("owner_user_id"),
                  "pending_owner": pending, "can_confirm_owner": bool(pending),
                  "can_enable_execution": bool(binding.get("account_id") and binding.get("owner_user_id")) and not binding.get("execution_requested", False),
                  "can_disable_execution": binding.get("execution_requested", False)}}
    try:
        if not binding.get("account_id"):
            result["accounts"] = _accounts()
            result["readiness"]["blockers"].append("ACCOUNT_SELECTION_REQUIRED")
        elif not binding.get("owner_user_id"):
            result["connection"]["status"] = "ACCOUNT_SELECTED"
            result["readiness"]["blockers"].append("PRIVATE_TELEGRAM_OWNER_REQUIRED")
        else:
            app = _application(connect, summary)
            with app._lock:
                detail = app.console_snapshot()
            result.update(detail)
            result["actions"].update(pause=True, resume=True, enable_execution=True, disable_execution=True)
            result["actions"]["prepare"] = result["actions"].get("prepare", False) and not binding.get("paused", True)
    except Exception as error:
        code = getattr(error, "code", None)
        result["readiness"]["blockers"].append(code if isinstance(code, str) else "BROKER_STATUS_TEMPORARILY_UNAVAILABLE")
    return result


def handle(method, path, payload, headers, connect, summary):
    """Return JSON, status, extra headers. All mutations are explicit POSTs."""
    if not enabled("VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED"):
        raise ConsoleError("CONSOLE_NOT_ENABLED", 503)
    if not isinstance(payload, Mapping):
        raise ConsoleError("JSON_OBJECT_REQUIRED", 400)
    repository = store_for(connect)
    if path.startswith(INTERNAL):
        if method != "POST":
            raise ConsoleError("METHOD_NOT_ALLOWED", 405)
        from veritas_currency_trade_service import _authenticate, _key_bytes
        _authenticate(headers, _key_bytes(os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_KEY", "")))
        operation = path[len(INTERNAL):]
        if operation == "health":
            binding = repository.binding()
            return {"ok": True, "environment": environment(),
                    "account_bound": bool(binding.get("account_id")),
                    "owner_bound": bool(binding.get("owner_user_id")),
                    "paused": binding.get("paused", True),
                    "execution_requested": binding.get("execution_requested", False)}, 200, {}
        if operation == "pair-owner":
            return repository.observe_owner(payload), 200, {}
        if operation == "owner-login":
            return repository.owner_login(payload), 200, {}
        if operation == "binding":
            binding = repository.binding()
            bid = positive_id(payload.get("bot_id"))
            if str(payload.get("bot_username", "")).lower() != EXPECTED_BOT:
                raise ConsoleError("WRONG_TRADE_BOT", 403)
            if binding.get("bot_id") not in (None, bid):
                raise ConsoleError("WRONG_TRADE_BOT", 403)
            return {"ok": True, "owner_user_id": binding.get("owner_user_id"), "bot_id": binding.get("bot_id"),
                    "paused": binding.get("paused", True), "revision": binding.get("revision", 0)}, 200, {}
        raise ConsoleError("CONSOLE_ENDPOINT_NOT_FOUND", 404)
    if method == "POST":
        if headers.get("Origin") != ORIGIN:
            raise ConsoleError("SAME_ORIGIN_REQUIRED", 403)
    operation = path[len(API):] if path.startswith(API) else ""
    if operation == "session" and method == "POST":
        try:
            expiry = datetime.fromisoformat(os.environ["VERITAS_CURRENCY_TRADE_SETUP_EXPIRES_AT"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            expiry = None
        # Validate session configuration before atomically consuming the grant.
        _csrf("configuration-check")
        session = repository.open_session(payload.get("setup_code"),
                    os.getenv("VERITAS_CURRENCY_TRADE_SETUP_HASH", ""), expiry)
        return {"authenticated": True, "csrf_token": _csrf(session)}, 200, {"Set-Cookie": session_cookie(session)}
    session = request_token(headers)
    actor = repository.authenticate(session)
    if method == "POST" and not hmac.compare_digest(str(headers.get("X-CSRF-Token", "")), _csrf(session)):
        raise ConsoleError("CSRF_TOKEN_REQUIRED", 403)
    if operation == "dashboard" and method == "GET":
        result = _dashboard(connect, summary, actor)
        result["csrf_token"] = _csrf(session)
        return result, 200, {}
    if operation == "setup" and method == "POST":
        action = payload.get("action")
        if action == "bind_account":
            account = clean_text(payload.get("account_id"))
            if not any(a["account_id"] == account and a["eligible"] for a in _accounts()):
                raise ConsoleError("EXACT_OPEN_FULL_ACCESS_ACCOUNT_REQUIRED")
            repository.select_account(account, actor)
            if repository.binding().get("owner_user_id"):
                _bind_ledger(connect, summary)
            return {"ok": True}, 200, {}
        if action == "pair_owner":
            return {"ok": True, "setup": repository.new_pairing(actor)}, 200, {}
        if action == "confirm_owner":
            repository.confirm_owner(payload.get("challenge_id"), actor)
            if repository.binding().get("account_id"):
                _bind_ledger(connect, summary)
            return {"ok": True}, 200, {}
        raise ConsoleError("INVALID_SETUP_ACTION", 400)
    if operation == "action" and method == "POST":
        action = payload.get("action")
        if action in ("pause", "resume", "enable_execution", "disable_execution"):
            repository.set_operation(action, actor)
            return {"ok": True}, 200, {}
        app = _application(connect, summary)
        with app._lock:
            app._initialize()
            if action == "reconcile":
                if not app.facts.is_bound():
                    app.facts.bind()
                return {"ok": True, "result": app.coordinator.reconcile()}, 200, {}
            if action == "prepare":
                if repository.binding().get("paused", True):
                    raise ConsoleError("PROPOSALS_PAUSED")
                return {"ok": True, "proposal": app._public(app.coordinator.prepare_next(), console=True)}, 200, {}
        raise ConsoleError("INVALID_CONSOLE_ACTION", 400)
    raise ConsoleError("CONSOLE_ENDPOINT_NOT_FOUND", 404)


def _reply(handler, body, code, headers=None, html=False):
    data = body.encode() if html else json.dumps(body, ensure_ascii=False, default=str).encode()
    handler.send_response(code)
    fixed = {"Content-Type": "text/html; charset=utf-8" if html else "application/json; charset=utf-8",
             "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
             "X-Frame-Options": "DENY", "Content-Length": str(len(data))}
    if html:
        fixed["Content-Security-Policy"] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    for key, value in {**fixed, **(headers or {})}.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def dispatch(handler, connect, summary):
    path = urlsplit(handler.path).path
    if path != PAGE and not path.startswith((API, INTERNAL, "/internal/currency-trading/", "/internal/currency-broker-alerts/")):
        return False
    if isinstance(summary, tuple) and len(summary) == 2:
        summary = summary_provider(*summary)
    try:
        if path == PAGE and handler.command == "GET":
            from veritas_currency_trade_ui import render_trading_ui
            _reply(handler, render_trading_ui(), 200, html=True)
            return True
        length = int(handler.headers.get("Content-Length", "0"))
        limit = 65536 if path in ("/internal/currency-trading/admission-evidence",
                                 "/internal/currency-trading/prepare-reviewed",
                                 "/internal/currency-trading/settlement-attest") else 8192
        if length < 0 or length > limit:
            raise ConsoleError("INVALID_BODY_SIZE", 400)
        body = json.loads(handler.rfile.read(length).decode()) if length else {}
        if path.startswith(("/internal/currency-trading/", "/internal/currency-broker-alerts/")):
            if handler.command != "POST":
                raise ConsoleError("METHOD_NOT_ALLOWED", 405)
            from veritas_currency_trade_service import handle_request, handle_broker_alerts_request
            if path.startswith("/internal/currency-broker-alerts/"):
                result, status = handle_broker_alerts_request(path, body, handler.headers, connect)
            else:
                result, status = handle_request(path, body, handler.headers, connect, summary)
            _reply(handler, result, status)
            return True
        result, status, headers = handle(handler.command, path, body, handler.headers, connect, summary)
        _reply(handler, result, status, headers)
    except (ConsoleError, ValueError, UnicodeError) as error:
        _reply(handler, {"ok": False, "code": getattr(error, "code", "INVALID_REQUEST")}, getattr(error, "status", 400))
    except Exception as error:
        code = getattr(error, "code", "CONSOLE_TEMPORARILY_UNAVAILABLE")
        safe = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "CONSOLE_TEMPORARILY_UNAVAILABLE"
        _reply(handler, {"ok": False, "code": safe}, 503)
    return True
