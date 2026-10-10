"""Console authorization and durable two-channel binding, with no external I/O.

SQLite runs the real SQL and transactions; only PostgreSQL syntax and returned
scalar types are translated. Optional PostgreSQL cases require the explicit
ci_ephemeral_test_only database and use a unique disposable schema.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import veritas_currency_trade_console as C
from test_veritas_currency_notifications import Connection

NOW = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
CODE = "isolated_setup_" + "x" * 32
OWNER, BOT = 123456789, 987654321
ENV = {"VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED": "1",
       "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "production",
       "VERITAS_CURRENCY_TRADE_SESSION_KEY": "isolated-session-test-key-32-bytes-long",
       "VERITAS_CURRENCY_TRADE_SERVICE_KEY": "isolated-service-test-key-32-bytes-long",
       "VERITAS_CURRENCY_TRADE_SETUP_HASH": C.digest(CODE),
       "VERITAS_CURRENCY_TRADE_SETUP_EXPIRES_AT": (NOW + timedelta(minutes=10)).isoformat()}
TABLE_NAMES = ("GRANTS", "SESSIONS", "CONFIG", "PAIRINGS", "AUDIT", "LOGINS")


class Cursor:
    def __init__(self, inner):
        self.inner = inner

    @staticmethod
    def decode(row):
        if row is None:
            return None
        for key, value in row.items():
            if key.endswith("_at") and isinstance(value, str):
                row[key] = datetime.fromisoformat(value)
            elif key in ("revoked", "paused", "execution_requested") and value is not None:
                row[key] = bool(value)
        return row

    def fetchone(self):
        return self.decode(self.inner.fetchone())

    def fetchall(self):
        return [self.decode(row) for row in self.inner.fetchall()]


class ConsoleConnection(Connection):
    def execute(self, sql, params=()):
        if sql.startswith("SET LOCAL "):
            return Cursor(super().execute("SELECT 1"))
        sql = sql.replace("public.", "").replace("FOR UPDATE OF l", "FOR UPDATE")
        return Cursor(super().execute(sql, params))


class Helpers:
    def initialize(self):
        self.now = NOW
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = C.ConsoleStore(self.connect, clock=lambda: self.now)
        self.store.ensure_schema()

    def open(self, code=CODE):
        return self.store.open_session(code, C.digest(CODE), NOW + timedelta(minutes=10))

    def proof(self, **changes):
        return {"user_id": OWNER, "private_chat_id": OWNER, "chat_type": "private",
                "is_bot": False, "bot_id": BOT, "bot_username": "AxednewsI_bot",
                "user_name": "Тестовый владелец", **changes}

    def pair(self, session):
        actor = self.store.authenticate(session)
        pairing = self.store.new_pairing(actor)
        code = pairing["pairing_url"].split("vt_", 1)[1]
        self.store.observe_owner(self.proof(pairing_code=code))
        return actor, pairing

    def bound(self):
        session = self.open()
        actor, pairing = self.pair(session)
        self.store.select_account("exact-account", actor)
        self.store.confirm_owner(pairing["challenge_id"], actor)
        return session, actor

    def login_code(self):
        return self.store.owner_login(self.proof())["login_url"].split("#setup=", 1)[1]

    def call(self, operation, body=None, session=None, method="POST", headers=None):
        h = {"Origin": C.ORIGIN}
        if session:
            h.update(Cookie=C.COOKIE + "=" + session, **{"X-CSRF-Token": C._csrf(session)})
        h.update(headers or {})
        with patch.object(C, "store_for", return_value=self.store):
            return C.handle(method, C.API + operation, body or {}, h, self.connect, lambda: [])


class ConsoleStoreTests(Helpers, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "console.sqlite")
        self.initialize()

    def connect(self):
        return ConsoleConnection(self.path)

    def test_single_use_bootstrap_and_hashed_storage(self):
        session = self.open()
        self.assertEqual(self.store.authenticate(session), C.digest(session))
        with self.assertRaisesRegex(C.ConsoleError, "ACCESS_CODE_ALREADY_USED"):
            self.open()
        with self.connect() as c:
            row = c.execute(f"SELECT * FROM {C.SESSIONS}").fetchone()
            self.assertEqual(row["environment"], "production")
            self.assertEqual(row["session_hash"], C.digest(session))
            for table in TABLE_NAMES:
                values = c.execute("SELECT * FROM " + getattr(C, table)).fetchall()
                rendered = json.dumps(values, default=str)
                self.assertNotIn(session, rendered)
                self.assertNotIn(CODE, rendered)

    def test_concurrent_bootstrap_consumption_creates_exactly_one_session(self):
        def attempt(_):
            try:
                return self.open()
            except C.ConsoleError as error:
                self.assertEqual(error.code, "ACCESS_CODE_ALREADY_USED")
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            sessions = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(s is not None for s in sessions), 1)

    def test_session_creation_audit_failure_rolls_back_grant_consumption(self):
        with patch.object(self.store, "_audit", side_effect=RuntimeError("isolated rollback")):
            with self.assertRaisesRegex(RuntimeError, "isolated rollback"):
                self.open()
        session = self.open()
        self.assertEqual(self.store.authenticate(session), C.digest(session))

    def test_expiry_and_environment_scope_are_enforced(self):
        self.now = NOW + timedelta(minutes=10)
        with self.assertRaisesRegex(C.ConsoleError, "ACCESS_CODE_EXPIRED"):
            self.open()
        self.now = NOW
        session = self.open()
        with patch.dict(os.environ, {"VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}):
            with self.assertRaisesRegex(C.ConsoleError, "SESSION_EXPIRED"):
                self.store.authenticate(session)
        self.now += timedelta(hours=12)
        with self.assertRaisesRegex(C.ConsoleError, "SESSION_EXPIRED"):
            self.store.authenticate(session)

    def test_two_channel_pairing_requires_matching_browser_and_private_owner(self):
        session = self.open()
        actor = self.store.authenticate(session)
        pairing = self.store.new_pairing(actor)
        with self.assertRaisesRegex(C.ConsoleError, "PAIRING_NOT_READY"):
            self.store.confirm_owner(pairing["challenge_id"], actor)
        code = pairing["pairing_url"].split("vt_", 1)[1]
        for changes in ({"chat_type": "channel"}, {"private_chat_id": OWNER + 1},
                        {"is_bot": True}, {"bot_username": "other_bot"}):
            with self.subTest(changes=changes), self.assertRaises(C.ConsoleError):
                self.store.observe_owner(self.proof(pairing_code=code, **changes))
        self.store.observe_owner(self.proof(pairing_code=code))
        self.assertIsNone(self.store.binding().get("owner_user_id"))
        self.assertEqual(self.store.pending_owner(actor)["user_id"], OWNER)
        with self.assertRaisesRegex(C.ConsoleError, "PAIRING_NOT_READY"):
            self.store.confirm_owner(pairing["challenge_id"], "different-session")
        self.store.confirm_owner(pairing["challenge_id"], actor)
        binding = self.store.binding()
        self.assertEqual((binding["owner_user_id"], binding["bot_id"]), (OWNER, BOT))
        self.assertTrue(binding["paused"])
        self.assertFalse(binding["execution_requested"])
        with self.assertRaisesRegex(C.ConsoleError, "PAIRING_NOT_READY"):
            self.store.confirm_owner(pairing["challenge_id"], actor)
        with self.connect() as c, c.transaction():
            c.execute(f"UPDATE {C.CONFIG} SET bot_id=%s WHERE environment=%s", (BOT + 1, "production"))
        with self.assertRaisesRegex(C.ConsoleError, "SESSION_EXPIRED"):
            self.store.authenticate(session)

    def test_pairing_expiry_and_candidate_takeover_are_rejected(self):
        session = self.open()
        actor, pairing = self.pair(session)
        code = pairing["pairing_url"].split("vt_", 1)[1]
        with self.assertRaisesRegex(C.ConsoleError, "PAIRING_ALREADY_CLAIMED"):
            self.store.observe_owner(self.proof(pairing_code=code, user_id=OWNER + 1, private_chat_id=OWNER + 1))
        self.now += timedelta(minutes=10)
        self.assertIsNone(self.store.pending_owner(actor))
        with self.assertRaisesRegex(C.ConsoleError, "PAIRING_NOT_READY"):
            self.store.confirm_owner(pairing["challenge_id"], actor)

    def test_exact_account_and_operation_switches_keep_approval_separate(self):
        _, actor = self.bound()
        with self.assertRaisesRegex(C.ConsoleError, "ACCOUNT_ALREADY_BOUND"):
            self.store.select_account("other-account", actor)
        self.store.set_operation("enable_execution", actor)
        self.assertTrue(self.store.binding()["paused"])
        self.assertTrue(self.store.binding()["execution_requested"])
        self.store.set_operation("resume", actor)
        self.assertFalse(self.store.binding()["paused"])
        self.store.set_operation("pause", actor)
        self.assertTrue(self.store.binding()["paused"])
        self.assertTrue(self.store.binding()["execution_requested"])
        self.store.set_operation("resume", actor)
        self.assertFalse(self.store.binding()["paused"])
        self.assertTrue(self.store.binding()["execution_requested"])
        self.store.set_operation("disable_execution", actor)
        self.assertFalse(self.store.binding()["execution_requested"])
        for action in ("approve", "execute", "decision", "send"):
            with self.subTest(action=action), self.assertRaisesRegex(C.ConsoleError, "INVALID_CONSOLE_ACTION"):
                self.store.set_operation(action, actor)

    def test_bound_owner_login_one_use_expiry_and_exact_identity(self):
        self.bound()
        for changes in ({"user_id": OWNER + 1, "private_chat_id": OWNER + 1},
                        {"bot_id": BOT + 1}, {"chat_type": "group"}):
            with self.subTest(changes=changes), self.assertRaises(C.ConsoleError):
                self.store.owner_login(self.proof(**changes))
        code = self.login_code()
        renewed = self.store.open_session(code, "", None)
        self.assertEqual(self.store.authenticate(renewed), C.digest(renewed))
        with self.assertRaisesRegex(C.ConsoleError, "INVALID_OR_EXPIRED_LOGIN_LINK"):
            self.store.open_session(code, "", None)
        old = self.login_code()
        newer = self.login_code()
        with self.assertRaisesRegex(C.ConsoleError, "INVALID_OR_EXPIRED_LOGIN_LINK"):
            self.store.open_session(old, "", None)
        self.now += timedelta(minutes=10)
        with self.assertRaisesRegex(C.ConsoleError, "INVALID_OR_EXPIRED_LOGIN_LINK"):
            self.store.open_session(newer, "", None)

    def test_concurrent_owner_login_consumption_and_owner_change(self):
        self.bound()
        code = self.login_code()
        def attempt(_):
            try:
                return self.store.open_session(code, "", None)
            except C.ConsoleError as error:
                self.assertEqual(error.code, "INVALID_OR_EXPIRED_LOGIN_LINK")
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            sessions = [s for s in pool.map(attempt, range(4)) if s]
        self.assertEqual(len(sessions), 1)
        with self.connect() as c, c.transaction():
            c.execute(f"UPDATE {C.CONFIG} SET owner_user_id=%s,owner_chat_id=%s WHERE environment=%s",
                      (OWNER + 1, OWNER + 1, "production"))
        with self.assertRaisesRegex(C.ConsoleError, "SESSION_EXPIRED"):
            self.store.authenticate(sessions[0])

    def test_http_origin_csrf_authentication_and_cookie_contract(self):
        with self.assertRaisesRegex(C.ConsoleError, "SAME_ORIGIN_REQUIRED"):
            self.call("session", {"setup_code": CODE}, headers={"Origin": "https://evil.test"})
        result, code, headers = self.call("session", {"setup_code": CODE})
        self.assertEqual(code, 200)
        cookie = SimpleCookie(); cookie.load(headers["Set-Cookie"])
        session = cookie[C.COOKIE].value
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, headers["Set-Cookie"])
        self.assertEqual(result["csrf_token"], C._csrf(session))
        with self.assertRaises(C.ConsoleError):
            self.call("dashboard", method="GET")
        with patch.object(C, "_accounts", return_value=[]):
            response, _, _ = self.call("dashboard", method="GET", session=session)
        self.assertTrue(response["authenticated"])
        self.assertFalse(response["actions"].get("prepare", False))
        with self.assertRaisesRegex(C.ConsoleError, "CSRF_TOKEN_REQUIRED"):
            self.call("setup", {"action": "pair_owner"}, session, headers={"X-CSRF-Token": "wrong"})

    def test_no_browser_individual_approval_endpoint_and_reconcile_recovers_binding(self):
        session, _ = self.bound()
        app = Mock(); app._lock = nullcontext(); app.facts.is_bound.return_value = False
        app.coordinator.reconcile.return_value = {"status": "RECONCILED"}
        with patch.object(C, "_application", return_value=app):
            for action in ("approve", "execute", "decision"):
                with self.subTest(action=action), self.assertRaisesRegex(C.ConsoleError, "INVALID_CONSOLE_ACTION"):
                    self.call("action", {"action": action}, session)
            result, _, _ = self.call("action", {"action": "reconcile"}, session)
        self.assertTrue(result["ok"])
        app.facts.bind.assert_called_once()
        app.coordinator.reconcile.assert_called_once()
        app.coordinator.execute_approved.assert_not_called()

    def test_exact_account_selection_and_auth_before_broker_access(self):
        session = self.open()
        with patch.object(C, "_accounts", return_value=[{"account_id": "exact", "eligible": True},
                                                       {"account_id": "readonly", "eligible": False}]) as accounts:
            with self.assertRaises(C.ConsoleError):
                self.call("setup", {"action": "bind_account", "account_id": "exact"})
            accounts.assert_not_called()
            for identifier in ("unknown", "readonly"):
                with self.assertRaisesRegex(C.ConsoleError, "EXACT_OPEN_FULL_ACCESS_ACCOUNT_REQUIRED"):
                    self.call("setup", {"action": "bind_account", "account_id": identifier}, session)
            self.call("setup", {"action": "bind_account", "account_id": "exact"}, session)
        self.assertEqual(self.store.binding()["account_id"], "exact")

    def test_private_internal_calls_require_service_key_and_exact_bot(self):
        self.bound()
        with patch.object(C, "store_for", return_value=self.store):
            with self.assertRaises(ValueError):
                C.handle("POST", C.INTERNAL + "binding", self.proof(), {}, self.connect, lambda: [])
            with self.assertRaisesRegex(C.ConsoleError, "WRONG_TRADE_BOT"):
                C.handle("POST", C.INTERNAL + "binding", self.proof(bot_id=BOT + 1),
                         {"X-Veritas-Trade-Key": ENV["VERITAS_CURRENCY_TRADE_SERVICE_KEY"]}, self.connect, lambda: [])


class DispatchTests(unittest.TestCase):
    def handler(self, path, method="GET", body=b"", headers=None):
        handler = Mock(path=path, command=method)
        handler.headers = {"Content-Length": str(len(body)), **(headers or {})}
        handler.rfile, handler.wfile = BytesIO(body), BytesIO()
        return handler

    def test_static_page_no_store_bounds_and_no_secret_database_access(self):
        connect = Mock(side_effect=AssertionError("Static page must not access DB"))
        handler = self.handler(C.PAGE)
        self.assertTrue(C.dispatch(handler, connect, lambda: []))
        handler.send_response.assert_called_with(200)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertFalse(C.dispatch(self.handler("/unrelated"), connect, lambda: []))
        for size in ("-1", "8193", "not-a-number"):
            with self.subTest(size=size):
                handler = self.handler(C.API + "action", "POST", headers={"Content-Length": size})
                C.dispatch(handler, connect, lambda: [])
                handler.send_response.assert_called_with(400)
        connect.assert_not_called()


@unittest.skipUnless(os.environ.get("VERITAS_TRADING_TEST_DSN"), "explicit isolated PostgreSQL DSN not configured")
class PostgresConsoleTests(Helpers, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        dsn = os.environ["VERITAS_TRADING_TEST_DSN"]
        if conninfo_to_dict(dsn).get("dbname") != "ci_ephemeral_test_only":
            raise RuntimeError("Console PostgreSQL tests require ci_ephemeral_test_only")
        cls.driver, cls.dict_row, cls.dsn = psycopg, dict_row, dsn
        cls.schema = "currency_console_test_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE SCHEMA " + cls.schema)
        cls.table_patch = patch.multiple(C, **{name: cls.schema + "." + getattr(C, name).split(".")[-1] for name in TABLE_NAMES})
        cls.table_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.table_patch.stop()
        with cls.driver.connect(cls.dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def connect(self):
        return self.driver.connect(self.dsn, autocommit=True, row_factory=type(self).dict_row)

    def setUp(self):
        self.initialize()
        with self.connect() as c, c.transaction():
            for name in TABLE_NAMES:
                c.execute("DELETE FROM " + getattr(C, name))

    test_parallel_bootstrap = ConsoleStoreTests.test_concurrent_bootstrap_consumption_creates_exactly_one_session
    test_parallel_owner_login = ConsoleStoreTests.test_concurrent_owner_login_consumption_and_owner_change
    test_atomic_audit_rollback = ConsoleStoreTests.test_session_creation_audit_failure_rolls_back_grant_consumption
    test_two_channel_binding = ConsoleStoreTests.test_two_channel_pairing_requires_matching_browser_and_private_owner

    def test_pause_ack_waits_for_send_guard_and_blocks_next_send(self):
        from veritas_currency_trade_plan import TradePlanBlocked
        _, actor = self.bound()
        self.store.set_operation("resume", actor)
        self.store.set_operation("enable_execution", actor)
        started = threading.Event()

        def pause():
            started.set()
            self.store.set_operation("pause", actor)
            return "PAUSE_ACKNOWLEDGED"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.execution_guard("exact-account", OWNER, OWNER, BOT) as allowed:
                self.assertIs(allowed, True)
                pending = pool.submit(pause)
                self.assertTrue(started.wait(1))
                # A pause cannot return success while another transaction holds
                # the CONFIG row through the final broker-send boundary.
                with self.assertRaises(FutureTimeoutError):
                    pending.result(timeout=.2)
            self.assertEqual(pending.result(timeout=3), "PAUSE_ACKNOWLEDGED")
        self.assertTrue(self.store.binding()["paused"])
        self.assertFalse(self.store.binding()["execution_requested"])
        with self.assertRaisesRegex(TradePlanBlocked, "EXECUTION_PAUSED_OR_SCOPE_CHANGED"):
            with self.store.execution_guard("exact-account", OWNER, OWNER, BOT):
                self.fail("A send guard entered after acknowledged pause")


if __name__ == "__main__":
    unittest.main()
