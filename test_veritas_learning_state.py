"""Cache commit semantics and durable job fencing; no production connection."""
import ast
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import veritas_learning_integrity as LI
import veritas_learning_exports as E
import veritas_learning_state as S
from veritas_maintenance import MaintenanceDeferred


class FakeConnection:
    def __init__(self, fail_commit=False):
        self.fail_commit = fail_commit
    @contextmanager
    def transaction(self):
        yield self
        if self.fail_commit:
            raise RuntimeError("commit failed")


NO_CHANGE = dict(staged=0, processed=0, pending=0, revoked=0)


class CacheCommitTests(unittest.TestCase):
    def setUp(self):
        self.saved = (LI._GENERATION, LI._REVOCATION_GENERATION, LI._UNCONFIRMED)
        LI._UNCONFIRMED = False
    def tearDown(self):
        LI._GENERATION, LI._REVOCATION_GENERATION, LI._UNCONFIRMED = self.saved

    def test_noop_preserves_ready_memory_only_after_commit(self):
        board = dict(E.memory_contract(), items=[{"n": 8}])
        before = LI.generation()
        with patch.object(LI, "_revalidate_eligible", return_value=NO_CHANGE):
            with LI.revalidation_transaction(FakeConnection()) as c:
                LI.revalidate_eligible(c)
                self.assertFalse(E.memory_current(board))
            self.assertEqual(LI.generation(), before)
            self.assertTrue(E.memory_current(board))

    def test_change_rollback_and_commit_failure_all_revoke_prior_profile(self):
        for kind in ("change", "rollback", "commit"):
            with self.subTest(kind=kind):
                LI._UNCONFIRMED = False
                board = dict(E.memory_contract(), items=[{"n": 8}])
                answer = dict(NO_CHANGE, staged=1) if kind=="change" else NO_CHANGE
                try:
                    with patch.object(LI, "_revalidate_eligible", return_value=answer):
                        with LI.revalidation_transaction(FakeConnection(kind=="commit")) as c:
                            LI.revalidate_eligible(c)
                            if kind=="rollback":
                                raise RuntimeError("rollback")
                except RuntimeError:
                    pass
                self.assertFalse(E.memory_current(board))
                if kind != "change":
                    self.assertFalse(LI.memory_state()["ready"])

    def test_concurrent_build_cannot_publish_intermediate_transaction_view(self):
        entered, finish = threading.Event(), threading.Event()
        board = dict(E.memory_contract(), items=["before"])
        errors = []
        def worker():
            try:
                with LI.revalidation_transaction(FakeConnection()) as c:
                    LI.revalidate_eligible(c)
                    entered.set()
                    if not finish.wait(2):
                        raise RuntimeError("test timeout")
            except BaseException as ex:
                errors.append(ex)
        with patch.object(LI, "_revalidate_eligible", return_value=dict(NO_CHANGE, processed=1)):
            thread=threading.Thread(target=worker); thread.start()
            self.assertTrue(entered.wait(2))
            during=dict(E.memory_contract(), items=["uncommitted view"])
            self.assertFalse(E.memory_current(board)); self.assertFalse(E.memory_current(during))
            finish.set(); thread.join(2)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertFalse(E.memory_current(board)); self.assertFalse(E.memory_current(during))
        self.assertTrue(E.memory_current(dict(E.memory_contract(), items=["committed rebuild"])))

    def test_previous_process_profile_never_restores_as_verified(self):
        board=dict(E.memory_contract(), items=[])
        board["learning_integrity_process_epoch"]="previous-process"
        self.assertFalse(E.memory_current(board))

    def test_additive_validation_does_not_revoke_prior_receipts_but_rewrites_and_errors_do(self):
        start=LI.memory_state();board=dict(E.memory_contract(),items=[])
        LI.invalidate('new_closed_trade',new_evidence_only=True)
        self.assertFalse(LI.memory_state()['ready'])
        with patch.object(LI,'_revalidate_eligible',return_value=dict(NO_CHANGE,staged=4,processed=4)):
            with LI.revalidation_transaction(FakeConnection()) as c:LI.revalidate_eligible(c)
        self.assertTrue(LI.memory_state()['ready'])
        self.assertGreater(LI.generation(),start['generation'])
        self.assertEqual(LI.memory_state()['revocation_generation'],start['revocation_generation'])
        self.assertFalse(E.memory_current(board))
        with patch.object(LI,'_revalidate_eligible',return_value=dict(NO_CHANGE,staged=1,processed=1,revoked=1)):
            with LI.revalidation_transaction(FakeConnection()) as c:LI.revalidate_eligible(c)
        rewrite=LI.memory_state()['revocation_generation']
        self.assertGreater(rewrite,start['revocation_generation'])
        with patch.object(LI,'_revalidate_eligible',return_value=NO_CHANGE):
            with self.assertRaisesRegex(RuntimeError,'commit failed'):
                with LI.revalidation_transaction(FakeConnection(True)) as c:LI.revalidate_eligible(c)
        self.assertGreater(LI.memory_state()['revocation_generation'],rewrite)
        self.assertFalse(LI.memory_state()['ready'])

    def test_errors_before_any_update_revoke_even_newly_built_memory(self):
        with patch.object(LI, "_revalidate_eligible", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                LI.revalidate_eligible(FakeConnection())
        self.assertFalse(E.memory_current(dict(E.memory_contract(), items=[])))
        with patch.object(LI, "_revalidate_eligible", return_value=NO_CHANGE):
            with LI.revalidation_transaction(FakeConnection()) as c:
                LI.revalidate_eligible(c)
        self.assertTrue(E.memory_current(dict(E.memory_contract(), items=[])))


class StateBoundsTests(unittest.TestCase):
    def test_bad_or_oversized_results_fail_before_connection(self):
        def forbidden():
            raise AssertionError("database touched before validation")
        for value in ({"status":"DEFERRED_MEMORY"},{"status":"ERROR"},
                      {"status":"OK","x":float("nan")},{"x":"a"*(S.MAX_SNAPSHOT_BYTES+1)}):
            with self.subTest(value=str(value)[:50]), self.assertRaises(ValueError):
                S.publish_snapshot(forbidden,"learning","V1",value)
        S._good({"status":"BUILDING"}); S._good({"status":"INSUFFICIENT_DATA"})

    def test_locked_snapshot_identity_and_write_hook_validate_before_sql(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("database touched before validation")
        c = SimpleNamespace(execute=forbidden)
        for saved in ({}, {"name": "other", "version": "V1"},
                      {"name": "model", "version": "V2"}, object()):
            with self.subTest(saved=saved), self.assertRaisesRegex(ValueError, "name/version mismatch"):
                S.publish_snapshot_in_transaction(c, "model", "V1", {"n": 1}, locked_snapshot=saved)
        for callback in (False, "invalid", object()):
            with self.subTest(callback=callback), self.assertRaisesRegex(ValueError, "must be callable"):
                S.publish_snapshot_in_transaction(c, "model", "V1", {"n": 1}, before_write=callback)

    def test_write_hook_deferral_preserves_original_object_without_a_write(self):
        saved = {"name": "model", "version": "V1"}
        for locked in (None, saved):
            with self.subTest(locked=locked is not None):
                reads, calls = [], []
                def execute(sql, args=None):
                    self.assertTrue(sql.lstrip().startswith("SELECT"), "write followed a deferred hook")
                    reads.append(sql)
                    return SimpleNamespace(fetchone=lambda: {"name": "model"})
                deferred = MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_before_write")
                def before_write():
                    calls.append(True)
                    raise deferred
                with self.assertRaises(MaintenanceDeferred) as caught:
                    S.publish_snapshot_in_transaction(SimpleNamespace(execute=execute), "model", "V1", {"n": 1},
                        locked_snapshot=locked, before_write=before_write)
                self.assertIs(caught.exception, deferred)
                self.assertEqual(calls, [True])
                self.assertEqual(len(reads), 0 if locked is not None else 2)


def _load_raw_connect(driver, *, dsn="synthetic-dsn", row_factory=None, schema=None):
    # Compile the production function alone; importing the monolith would start
    # unrelated runtime/bootstrap code. Native tests replace only its two
    # schema-name literals with an isolated, generated test namespace.
    source = Path(__file__).with_name("veritas_intelligence.py")
    module = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in module.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_v90_pg_raw_connect_impl")
    if schema is not None:
        if not schema.startswith("connection_setup_test_") or not schema.removeprefix("connection_setup_test_").isalnum():
            raise AssertionError("unsafe test schema")
        replaced = 0
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                replaced += node.value.count("veritas_v90")
                node.value = node.value.replace("veritas_v90", schema)
        if replaced != 2:
            raise AssertionError("unexpected production schema references")
    namespace = {"DATABASE_URL": dsn, "psycopg": driver, "dict_row": row_factory}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_v90_pg_raw_connect_impl"]


class RawConnectionSetupTests(unittest.TestCase):
    def test_setup_keeps_connection_options_and_returns_the_open_connection(self):
        calls, statements, closed = [], [], []
        connection = SimpleNamespace(execute=lambda sql: statements.append(sql),
                                     close=lambda: closed.append(True))
        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            return connection
        row_factory = object()
        setup = _load_raw_connect(SimpleNamespace(connect=connect), row_factory=row_factory)
        self.assertIs(setup(), connection)
        self.assertEqual(calls, [(("synthetic-dsn",),
                                 {"autocommit": True, "row_factory": row_factory, "connect_timeout": 6})])
        self.assertEqual(statements,
                         ["CREATE SCHEMA IF NOT EXISTS veritas_v90; SET search_path TO veritas_v90"])
        self.assertEqual(closed, [])

    def test_failed_setup_closes_connection_without_masking_the_original_error(self):
        for close_fails in (False, True):
            for error in (RuntimeError("synthetic setup failure"), KeyboardInterrupt("synthetic interruption")):
                with self.subTest(close_fails=close_fails, error=type(error).__name__):
                    closed = []
                    def execute(sql):
                        raise error
                    def close():
                        closed.append(True)
                        if close_fails:
                            raise RuntimeError("synthetic cleanup failure")
                    connection = SimpleNamespace(execute=execute, close=close)
                    setup = _load_raw_connect(SimpleNamespace(connect=lambda *args, **kwargs: connection))
                    with self.assertRaises(type(error)) as caught:
                        setup()
                    self.assertIs(caught.exception, error)
                    self.assertEqual(closed, [True])


DSN=os.getenv("VERITAS_QUALITY_TEST_DSN","")


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class RawConnectionSetupSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver, self.row_factory = psycopg, dict_row
        self.schema = "connection_setup_test_"+uuid.uuid4().hex
        with psycopg.connect(DSN, autocommit=True) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
        self.addCleanup(self._drop_schema)

    def _drop_schema(self):
        with self.driver.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")

    def _setup(self, *, fail_second=False):
        connections = []
        case = self
        class ObservedConnection:
            def __init__(self, raw):
                self.raw, self.setup_result, self.setup_error = raw, None, None
            def __getattr__(self, name):
                return getattr(self.raw, name)
            def execute(self, sql):
                if fail_second:
                    first, separator, second = sql.partition(";")
                    case.assertEqual(first, "CREATE SCHEMA IF NOT EXISTS "+case.schema)
                    case.assertEqual(separator, ";")
                    case.assertEqual(second.strip(), "SET search_path TO "+case.schema)
                    # This is valid SQL syntax but an invalid GUC value. The
                    # first CREATE really executes before the second SET fails.
                    sql = first+"; SET lock_timeout = 'synthetic-invalid-duration'"
                try:
                    result = self.raw.execute(sql)
                except BaseException as error:
                    self.setup_error = error
                    raise
                if self.setup_result is None:
                    self.setup_result = result
                return result
        def connect(*args, **kwargs):
            connection = ObservedConnection(case.driver.connect(*args, **kwargs))
            connections.append(connection)
            case.addCleanup(connection.close)
            return connection
        setup = _load_raw_connect(SimpleNamespace(connect=connect), dsn=DSN,
                                  row_factory=self.row_factory, schema=self.schema)
        return setup, connections

    def test_native_setup_commits_schema_and_session_path_before_next_sql(self):
        from psycopg.pq import ExecStatus, TransactionStatus
        setup, connections = self._setup()
        c = setup()
        self.assertIs(c, connections[0])
        self.assertTrue(c.autocommit)
        self.assertFalse(c.closed)
        self.assertEqual(c.info.transaction_status, TransactionStatus.IDLE)
        # Issue real subsequent queries before inspecting either setup result:
        # production must not require a caller to drain results with nextset().
        self.assertEqual(c.execute("SHOW search_path").fetchone()["search_path"], self.schema)
        self.assertEqual(c.execute("SELECT current_schema() AS schema").fetchone()["schema"], self.schema)
        c.execute("CREATE TABLE setup_probe (value integer)")
        c.execute("INSERT INTO setup_probe VALUES (7)")
        with self.assertRaisesRegex(RuntimeError, "synthetic caller rollback"):
            with c.transaction():
                c.execute("INSERT INTO setup_probe VALUES (9)")
                raise RuntimeError("synthetic caller rollback")
        self.assertEqual(c.info.transaction_status, TransactionStatus.IDLE)
        self.assertEqual(c.execute("SHOW search_path").fetchone()["search_path"], self.schema)
        with self.driver.connect(DSN, autocommit=True) as observer:
            self.assertEqual(observer.execute(f"SELECT value FROM {self.schema}.setup_probe").fetchall(), [(7,)])
        results = []
        while True:
            results.append((c.setup_result.pgresult.status, c.setup_result.statusmessage))
            if not c.setup_result.nextset():
                break
        self.assertEqual(results, [(ExecStatus.COMMAND_OK, "CREATE SCHEMA"), (ExecStatus.COMMAND_OK, "SET")])

    def test_native_second_setup_failure_rolls_back_new_schema_and_closes_connection(self):
        for existed in (False, True):
            with self.subTest(existing_schema=existed):
                if existed:
                    with self.driver.connect(DSN, autocommit=True) as observer:
                        observer.execute(f"CREATE SCHEMA {self.schema}")
                        observer.execute(f"CREATE TABLE {self.schema}.preserved (value integer)")
                        observer.execute(f"INSERT INTO {self.schema}.preserved VALUES (11)")
                setup, connections = self._setup(fail_second=True)
                with self.assertRaises(self.driver.errors.InvalidParameterValue) as caught:
                    setup()
                self.assertIs(caught.exception, connections[0].setup_error)
                self.assertTrue(connections[0].closed)
                self.assertIn("lock_timeout", caught.exception.diag.message_primary)
                with self.driver.connect(DSN, autocommit=True) as observer:
                    present = observer.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)",
                                               (self.schema,)).fetchone()[0]
                    self.assertEqual(present, existed)
                    if existed:
                        self.assertEqual(observer.execute(f"SELECT value FROM {self.schema}.preserved").fetchall(), [(11,)])


@unittest.skipUnless(DSN,"isolated PostgreSQL test database not configured")
class DurableStateSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver,self.row_factory=psycopg,dict_row
        self.schema="learning_state_test_"+uuid.uuid4().hex
        with psycopg.connect(DSN,autocommit=True) as c:
            if c.execute("SELECT current_database()").fetchone()[0]!="veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {self.schema}")
        S.ensure_schema(self.connect)
    @contextmanager
    def connect(self):
        with self.driver.connect(DSN,autocommit=True,row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c
    def tearDown(self):
        with self.driver.connect(DSN,autocommit=True) as c:
            c.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def _assert_timeouts(self, c, statement, lock):
        self.assertEqual(c.execute("SHOW statement_timeout").fetchone()["statement_timeout"], statement)
        self.assertEqual(c.execute("SHOW lock_timeout").fetchone()["lock_timeout"], lock)

    def test_transaction_timeouts_are_exact_and_local_after_commit_and_rollback(self):
        from psycopg.pq import ExecStatus
        # Reuse the physical connection so closing it cannot hide a leaked GUC.
        with self.connect() as raw:
            raw.execute("SET statement_timeout='9s'")
            raw.execute("SET lock_timeout='7s'")
            raw.execute("CREATE TEMP TABLE timeout_commit_probe (id text PRIMARY KEY)")
            for rollback in (False, True):
                with self.subTest(rollback=rollback):
                    results = []
                    class ObservedConnection:
                        def transaction(self):
                            return raw.transaction()
                        def execute(self, sql, args=None):
                            result = raw.execute(sql, args) if args is not None else raw.execute(sql)
                            if not results:
                                while True:
                                    results.append((result.pgresult.status, result.statusmessage))
                                    if not result.nextset():
                                        break
                            return result
                    expected = self.assertRaisesRegex(RuntimeError, "synthetic settings rollback") if rollback else nullcontext()
                    with expected, S._transaction(lambda: nullcontext(ObservedConnection())) as c:
                        self.assertEqual(results, [(ExecStatus.COMMAND_OK, "SET"), (ExecStatus.COMMAND_OK, "SET")])
                        self._assert_timeouts(c, "2s", "250ms")
                        c.execute("INSERT INTO timeout_commit_probe VALUES (%s)", ("rollback" if rollback else "commit",))
                        if rollback:
                            raise RuntimeError("synthetic settings rollback")
                    self._assert_timeouts(raw, "9s", "7s")
                    self.assertEqual(raw.execute("SELECT id FROM timeout_commit_probe").fetchall(), [{"id": "commit"}])

    def test_second_timeout_setup_error_rolls_back_first_setting_without_entering_body(self):
        with self.connect() as raw:
            raw.execute("SET statement_timeout='9s'")
            raw.execute("SET lock_timeout='7s'")
            class InvalidSecondSetting:
                def transaction(self):
                    return raw.transaction()
                def execute(self, sql, args=None):
                    # Leave the first actual utility command intact and make
                    # only the second fail on PostgreSQL, not in a SQL mock.
                    first, separator, _ = sql.partition(";")
                    if not separator or args is not None:
                        raise AssertionError("expected an unparameterized settings pair")
                    return raw.execute(first+"; SET LOCAL lock_timeout='synthetic-invalid-timeout'")
            entered = False
            with self.assertRaises(self.driver.errors.InvalidParameterValue):
                with S._transaction(lambda: nullcontext(InvalidSecondSetting())):
                    entered = True
            self.assertFalse(entered)
            self._assert_timeouts(raw, "9s", "7s")
            self.assertEqual(raw.execute("SELECT 7 AS healthy").fetchone()["healthy"], 7)

    def test_transaction_timeouts_cancel_sql_and_lock_wait_without_leaking_settings(self):
        with self.connect() as raw:
            raw.execute("SET statement_timeout='9s'")
            raw.execute("SET lock_timeout='7s'")
            raw.execute("CREATE TEMP TABLE timeout_rollback_probe (id integer PRIMARY KEY)")
            with self.assertRaises(self.driver.errors.QueryCanceled):
                with S._transaction(lambda: nullcontext(raw)) as c:
                    c.execute("INSERT INTO timeout_rollback_probe VALUES (1)")
                    c.execute("SELECT pg_sleep(3)")
            self._assert_timeouts(raw, "9s", "7s")
            self.assertEqual(raw.execute("SELECT count(*) AS n FROM timeout_rollback_probe").fetchone()["n"], 0)
            # The unchanged snapshot lock must still time out at 250ms instead
            # of waiting for the longer statement cap or altering lock order.
            with self.connect() as holder, holder.transaction():
                holder.execute("SELECT pg_advisory_xact_lock(%s)", (S._LOCK,))
                with self.assertRaises(self.driver.errors.LockNotAvailable):
                    with S._transaction(lambda: nullcontext(raw)) as c:
                        c.execute("INSERT INTO timeout_rollback_probe VALUES (2)")
                        S.load_snapshot_in_transaction(c, "timeout-probe", "V1", for_update=True)
            self._assert_timeouts(raw, "9s", "7s")
            self.assertEqual(raw.execute("SELECT count(*) AS n FROM timeout_rollback_probe").fetchone()["n"], 0)

    def test_good_only_monotonic_idempotent_snapshots_survive_reconnect(self):
        at=datetime.now(timezone.utc); value={"status":"BUILDING","n":0}
        self.assertTrue(S.publish_snapshot(self.connect,"progress","V1",value,observed_at=at,watermark="a"))
        first=S.load_snapshot(self.connect,"progress","V1")
        self.assertTrue(S.publish_snapshot(self.connect,"progress","V1",value,observed_at=at,watermark="a"))
        self.assertEqual(S.load_snapshot(self.connect,"progress","V1"),first)
        self.assertFalse(S.publish_snapshot(self.connect,"progress","V1",{"n":999},observed_at=at-timedelta(seconds=1)))
        with self.assertRaises(ValueError):
            S.publish_snapshot(self.connect,"progress","V1",{"status":"ERROR"})
        self.assertEqual(S.load_snapshot(self.connect,"progress","V1"),first)
        self.assertIsNone(S.load_snapshot(self.connect,"progress","V2"))
        self.assertIsNone(S.load_snapshot(self.connect,"progress","V1",max_age_seconds=0))

    def test_lease_fences_old_worker_and_preserves_last_good_after_error(self):
        lease=S.claim_job(self.connect,"job","V1")
        self.assertIsNotNone(lease); self.assertIsNone(S.claim_job(self.connect,"job","V1"))
        self.assertTrue(S.checkpoint_job(self.connect,lease,status="OK",cursor={"after":7},result={"n":7}))
        next_lease=S.claim_job(self.connect,"job","V1")
        self.assertGreater(next_lease["fence"],lease["fence"])
        self.assertEqual(next_lease["cursor"],{"after":7})
        self.assertFalse(S.checkpoint_job(self.connect,lease,status="OK",result={"n":999}))
        self.assertFalse(S.publish_snapshot(self.connect,"candidate","V1",{"n":999},lease=lease))
        self.assertTrue(S.checkpoint_job(self.connect,next_lease,status="ERROR",result={"error":"temporary"}))
        state=S.job_state(self.connect,"job","V1")
        self.assertEqual(state["last_good"],{"n":7})
        self.assertEqual(state["status"],"ERROR")

    def test_expired_lease_and_version_change_restart_safely(self):
        old=S.claim_job(self.connect,"job","V1")
        with self.connect() as c:
            c.execute("UPDATE veritas_learning_jobs SET lease_until=clock_timestamp()-interval '1 second'")
        new=S.claim_job(self.connect,"job","V2")
        self.assertIsNotNone(new); self.assertIsNone(new["cursor"])
        self.assertFalse(S.checkpoint_job(self.connect,old,status="OK"))
        self.assertTrue(S.checkpoint_job(self.connect,new,status="BUILDING",result={"status":"BUILDING"},retry_after_seconds=30))
        self.assertIsNone(S.claim_job(self.connect,"job","V2"))

    def test_atomic_dedup_and_snapshot_rollback_together(self):
        with self.connect() as c:
            c.execute("CREATE TABLE seen (id text PRIMARY KEY)")
            with self.assertRaisesRegex(RuntimeError,"after publish"):
                with c.transaction():
                    S.load_snapshot_in_transaction(c,"candidate","V1",for_update=True)
                    c.execute("INSERT INTO seen VALUES ('e1')")
                    S.publish_snapshot_in_transaction(c,"candidate","V1",{"n":1})
                    raise RuntimeError("after publish")
            self.assertEqual(c.execute("SELECT count(*) AS n FROM seen").fetchone()["n"],0)
        self.assertIsNone(S.load_snapshot(self.connect,"candidate","V1"))

    def test_capacity_never_evicts_existing_snapshot(self):
        S.publish_snapshot(self.connect,"first","V1",{"n":1})
        with patch.object(S,"MAX_SNAPSHOTS",1):
            with self.assertRaises(ValueError):
                S.publish_snapshot(self.connect,"second","V1",{"n":2})
            self.assertTrue(S.publish_snapshot(self.connect,"first","V1",{"n":3}))
        self.assertEqual(S.load_snapshot(self.connect,"first","V1")["payload"],{"n":3})

    def test_locked_snapshot_updates_full_slot_at_capacity_with_monotonic_idempotent_state(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        old_clock = datetime(2020, 1, 1, tzinfo=timezone.utc)
        initial = {"status": "BUILDING", "offset": 0}
        encoded = S._json(initial, S.MAX_SNAPSHOT_BYTES)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connect() as raw:
            raw.execute("""INSERT INTO veritas_learning_snapshots
                (name,version,payload,payload_hash,observed_at,updated_at)
                SELECT 'slot-'||n::text,'V1',%s::jsonb,%s,%s,%s
                FROM generate_series(0,31) AS fixture(n)""", (encoded,digest,at,old_clock))
            self.assertEqual(raw.execute("SELECT count(*) AS n FROM veritas_learning_snapshots").fetchone()["n"], 32)
        value = {"status": "BUILDING", "offset": 2,
                 "pairs": [[n, n+1] for n in range(1, 2201)],
                 "episodes": [{"n": 1, "details": {"kept": True}}], "previous": [0.25, -0.5]}
        value["pairs"][1] = value["pairs"][0][:]
        with S._transaction(self.connect) as c:
            saved = S.load_snapshot_in_transaction(c, "slot-0", "V1", for_update=True)
            operations = []
            def execute(sql, args=None):
                operations.append(sql.lstrip().split(None, 1)[0])
                return c.execute(sql, args) if args is not None else c.execute(sql)
            self.assertTrue(S.publish_snapshot_in_transaction(SimpleNamespace(execute=execute), "slot-0", "V1", value,
                observed_at=at, locked_snapshot=saved, before_write=lambda: operations.append("HOOK")))
            self.assertEqual(operations, ["HOOK", "UPDATE"])
            changed = c.execute("SELECT * FROM veritas_learning_snapshots WHERE name='slot-0'").fetchone()
            self.assertEqual(changed["payload"], value)
            self.assertEqual(changed["payload_hash"], hashlib.sha256(S._json(value, S.MAX_SNAPSHOT_BYTES).encode("utf-8")).hexdigest())
            self.assertGreater(changed["updated_at"], old_clock)
            # Compare the actual stored values with the unchanged generic path.
            self.assertTrue(S.publish_snapshot_in_transaction(c, "slot-1", "V1", value, observed_at=at))
            generic = c.execute("SELECT * FROM veritas_learning_snapshots WHERE name='slot-1'").fetchone()
            self.assertEqual({k:v for k,v in changed.items() if k not in ("name", "updated_at")},
                             {k:v for k,v in generic.items() if k not in ("name", "updated_at")})
            self.assertTrue(S.publish_snapshot_in_transaction(c, "slot-0", "V1", value,
                observed_at=at, locked_snapshot=saved))
            self.assertEqual(c.execute("SELECT * FROM veritas_learning_snapshots WHERE name='slot-0'").fetchone(), changed)
            self.assertFalse(S.publish_snapshot_in_transaction(c, "slot-0", "V1", {"offset": 999},
                observed_at=at-timedelta(seconds=1), locked_snapshot=saved))
            self.assertEqual(c.execute("SELECT * FROM veritas_learning_snapshots WHERE name='slot-0'").fetchone(), changed)
            for watermark, observed in (("slice-a", at), (None, at), (None, at+timedelta(seconds=1))):
                c.execute("UPDATE veritas_learning_snapshots SET updated_at=%s WHERE name='slot-0'", (old_clock,))
                self.assertTrue(S.publish_snapshot_in_transaction(c, "slot-0", "V1", value,
                    watermark=watermark, observed_at=observed, locked_snapshot=saved))
                row = c.execute("SELECT * FROM veritas_learning_snapshots WHERE name='slot-0'").fetchone()
                self.assertEqual(row["payload"], value)
                self.assertEqual(row["payload_hash"], changed["payload_hash"])
                self.assertEqual(row["watermark"], watermark)
                self.assertEqual(row["observed_at"], observed)
                self.assertGreater(row["updated_at"], old_clock)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM veritas_learning_snapshots").fetchone()["n"], 32)
        calls = []
        with self.assertRaisesRegex(ValueError, "capacity reached"), S._transaction(self.connect) as c:
            S.publish_snapshot_in_transaction(c, "overflow", "V1", {"n": 1}, before_write=lambda: calls.append(True))
        self.assertEqual(calls, [])
        self.assertEqual(S.load_snapshot(self.connect, "slot-0", "V1")["payload"], value)

    def test_locked_snapshot_rejects_wrong_identity_missing_row_and_changed_version_without_insert(self):
        self.assertTrue(S.publish_snapshot(self.connect, "model", "V1", {"n": 1}))
        self.assertTrue(S.publish_snapshot(self.connect, "other", "V1", {"n": 2}))
        with self.connect() as raw:
            original = raw.execute("SELECT * FROM veritas_learning_snapshots ORDER BY name").fetchall()
            for wrong in ({"name": "other", "version": "V1"}, {"name": "model", "version": "V2"}):
                with self.subTest(wrong=wrong), raw.transaction():
                    S.load_snapshot_in_transaction(raw, "model", "V1", for_update=True)
                    with self.assertRaisesRegex(ValueError, "name/version mismatch"):
                        S.publish_snapshot_in_transaction(raw, "model", "V1", {"n": 999}, locked_snapshot=wrong)
                    self.assertEqual(raw.execute("SELECT * FROM veritas_learning_snapshots ORDER BY name").fetchall(), original)
            for mutation in ("DELETE FROM veritas_learning_snapshots WHERE name='model'",
                             "UPDATE veritas_learning_snapshots SET version='V2' WHERE name='model'"):
                with self.subTest(mutation=mutation):
                    with self.assertRaisesRegex(RuntimeError, "restore synthetic mutation"), raw.transaction():
                        saved = S.load_snapshot_in_transaction(raw, "model", "V1", for_update=True)
                        raw.execute(mutation)
                        mutated = raw.execute("SELECT * FROM veritas_learning_snapshots ORDER BY name").fetchall()
                        self.assertFalse(S.publish_snapshot_in_transaction(raw, "model", "V1", {"n": 999}, locked_snapshot=saved))
                        self.assertEqual(raw.execute("SELECT * FROM veritas_learning_snapshots ORDER BY name").fetchall(), mutated)
                        raise RuntimeError("restore synthetic mutation")
                    self.assertEqual(raw.execute("SELECT * FROM veritas_learning_snapshots ORDER BY name").fetchall(), original)
            # A version-filtered miss still uses normal publication semantics:
            # the existing named slot can change version without new capacity.
            with raw.transaction():
                self.assertIsNone(S.load_snapshot_in_transaction(raw, "model", "V2", for_update=True))
                self.assertTrue(S.publish_snapshot_in_transaction(raw, "model", "V2", {"n": 3}, locked_snapshot=None))
            self.assertEqual(raw.execute("SELECT count(*) AS n FROM veritas_learning_snapshots").fetchone()["n"], 2)
        self.assertEqual(S.load_snapshot(self.connect, "model", "V2")["payload"], {"n": 3})

    def test_locked_snapshot_retains_global_row_and_table_locks_until_commit_or_rollback(self):
        from psycopg.pq import TransactionStatus
        self.assertTrue(S.publish_snapshot(self.connect, "model", "V1", {"n": 1}))
        with self.connect() as raw, self.connect() as competitor:
            for rollback in (False, True):
                with self.subTest(rollback=rollback):
                    original = raw.execute("SELECT * FROM veritas_learning_snapshots WHERE name='model'").fetchone()
                    expected = self.assertRaisesRegex(RuntimeError, "synthetic locked rollback") if rollback else nullcontext()
                    with expected, S._transaction(lambda: nullcontext(raw)) as c:
                        saved = S.load_snapshot_in_transaction(c, "model", "V1", for_update=True)
                        for phase in ("before", "after"):
                            with competitor.transaction():
                                self.assertFalse(competitor.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (S._LOCK,)).fetchone()["ok"])
                                with self.assertRaises(self.driver.errors.LockNotAvailable), competitor.transaction():
                                    competitor.execute("SELECT name FROM veritas_learning_snapshots WHERE name='model' FOR UPDATE NOWAIT")
                                with self.assertRaises(self.driver.errors.LockNotAvailable), competitor.transaction():
                                    competitor.execute("LOCK TABLE veritas_learning_snapshots IN ACCESS EXCLUSIVE MODE NOWAIT")
                            self.assertEqual(c.info.transaction_status, TransactionStatus.INTRANS)
                            if phase == "before":
                                self.assertTrue(S.publish_snapshot_in_transaction(c, "model", "V1", {"n": 3 if rollback else 2},
                                    locked_snapshot=saved))
                        if rollback:
                            raise RuntimeError("synthetic locked rollback")
                    self.assertEqual(raw.info.transaction_status, TransactionStatus.IDLE)
                    with competitor.transaction():
                        self.assertTrue(competitor.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (S._LOCK,)).fetchone()["ok"])
                        self.assertIsNotNone(competitor.execute("SELECT name FROM veritas_learning_snapshots WHERE name='model' FOR UPDATE NOWAIT").fetchone())
                        competitor.execute("LOCK TABLE veritas_learning_snapshots IN ACCESS EXCLUSIVE MODE NOWAIT")
                    row = raw.execute("SELECT * FROM veritas_learning_snapshots WHERE name='model'").fetchone()
                    if rollback:
                        self.assertEqual(row, original)
                    else:
                        self.assertEqual(row["payload"], {"n": 2})

    def test_locked_snapshot_lease_and_write_hook_preserve_fences_rollback_and_sql_errors(self):
        from psycopg.pq import TransactionStatus
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        value = {"n": 1}
        self.assertTrue(S.publish_snapshot(self.connect, "model", "V1", value, observed_at=at))
        old = S.claim_job(self.connect, "publisher", "V1", owner="old-worker")
        self.assertTrue(S.checkpoint_job(self.connect, old, status="OK", cursor={"offset": 7}, result={"n": 7}))
        lease = S.claim_job(self.connect, "publisher", "V1", owner="current-worker")
        self.assertGreater(lease["fence"], old["fence"])
        before_job = S.job_state(self.connect, "publisher", "V1")
        with S._transaction(self.connect) as c:
            saved = S.load_snapshot_in_transaction(c, "model", "V1", for_update=True)
            calls = []
            self.assertFalse(S.publish_snapshot_in_transaction(c, "model", "V1", {"n": 999}, lease=old,
                locked_snapshot=saved, before_write=lambda: calls.append(True)))
            self.assertEqual(calls, [])
            self.assertTrue(S.publish_snapshot_in_transaction(c, "model", "V1", value, observed_at=at, lease=lease,
                locked_snapshot=saved, before_write=lambda: calls.append(True)))
            self.assertEqual(calls, [True])
        self.assertEqual(S.job_state(self.connect, "publisher", "V1"), before_job)
        with self.connect() as raw:
            raw.execute("UPDATE veritas_learning_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE name='publisher'")
            original = raw.execute("SELECT * FROM veritas_learning_snapshots WHERE name='model'").fetchone()
            with S._transaction(lambda: nullcontext(raw)) as c:
                saved = S.load_snapshot_in_transaction(c, "model", "V1", for_update=True)
                calls = []
                self.assertFalse(S.publish_snapshot_in_transaction(c, "model", "V1", {"n": 999}, lease=lease,
                    locked_snapshot=saved, before_write=lambda: calls.append(True)))
                self.assertEqual(calls, [])
            self.assertEqual(raw.execute("SELECT * FROM veritas_learning_snapshots WHERE name='model'").fetchone(), original)
            raw.execute("CREATE TABLE checkpoint_seen (id integer PRIMARY KEY)")
            for locked in (False, True):
                with self.subTest(locked=locked):
                    deferred = MaintenanceDeferred("DEFERRED_TIME_BUDGET", reason="synthetic_before_snapshot_write")
                    calls = []
                    def before_write():
                        calls.append(True)
                        raise deferred
                    with self.assertRaises(MaintenanceDeferred) as caught:
                        with S._transaction(lambda: nullcontext(raw)) as c:
                            saved = S.load_snapshot_in_transaction(c, "model", "V1", for_update=True) if locked else None
                            c.execute("INSERT INTO checkpoint_seen VALUES (1)")
                            S.publish_snapshot_in_transaction(c, "model", "V1", {"n": 999},
                                locked_snapshot=saved, before_write=before_write)
                    self.assertIs(caught.exception, deferred)
                    self.assertEqual(calls, [True])
                    self.assertEqual(raw.info.transaction_status, TransactionStatus.IDLE)
                    self.assertEqual(raw.execute("SELECT * FROM veritas_learning_snapshots WHERE name='model'").fetchone(), original)
                    self.assertEqual(raw.execute("SELECT count(*) AS n FROM checkpoint_seen").fetchone()["n"], 0)
            raw.execute("""ALTER TABLE veritas_learning_snapshots ADD CONSTRAINT synthetic_write_rejection
                CHECK (NOT (payload ? 'reject_write'))""")
            calls, sql_errors = [], []
            def execute(sql, args=None):
                try:
                    return raw.execute(sql, args) if args is not None else raw.execute(sql)
                except Exception as error:
                    sql_errors.append(error)
                    raise
            with self.assertRaises(self.driver.errors.CheckViolation) as caught:
                with S._transaction(lambda: nullcontext(raw)) as c:
                    saved = S.load_snapshot_in_transaction(c, "model", "V1", for_update=True)
                    c.execute("INSERT INTO checkpoint_seen VALUES (2)")
                    S.publish_snapshot_in_transaction(SimpleNamespace(execute=execute), "model", "V1", {"reject_write": True},
                        locked_snapshot=saved, before_write=lambda: calls.append(True))
            self.assertEqual(calls, [True])
            self.assertEqual(sql_errors, [caught.exception])
            self.assertEqual(caught.exception.diag.constraint_name, "synthetic_write_rejection")
            self.assertEqual(raw.info.transaction_status, TransactionStatus.IDLE)
            self.assertEqual(raw.execute("SELECT * FROM veritas_learning_snapshots WHERE name='model'").fetchone(), original)
            self.assertEqual(raw.execute("SELECT count(*) AS n FROM checkpoint_seen").fetchone()["n"], 0)


    def _thread_call(self, operation):
        values, errors = [], []
        def run():
            try:
                values.append(operation())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, values, errors

    def test_existing_job_claim_and_checkpoint_ignore_snapshot_global_holder(self):
        old = S.claim_job(self.connect, "existing", "V1", owner="old-worker")
        self.assertTrue(S.checkpoint_job(self.connect, old, status="OK",
            cursor={"after": 7}, result={"status": "OK", "n": 7}))
        self.assertTrue(S.publish_snapshot(self.connect, "model", "V1", {"n": 7}))
        original_model = S.load_snapshot(self.connect, "model", "V1")
        with self.connect() as holder, holder.transaction():
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (S._LOCK,))
            # Another real connection must progress while the snapshot lock is
            # held. The old implementation raises LockNotAvailable here.
            lease = S.claim_job(self.connect, "existing", "V1", owner="new-worker")
            self.assertIsNotNone(lease)
            self.assertGreater(lease["fence"], old["fence"])
            self.assertEqual(lease["cursor"], {"after": 7})
            self.assertEqual(lease["last_good"], {"status": "OK", "n": 7})
            self.assertTrue(S.checkpoint_job(self.connect, lease, status="OK",
                cursor={"after": 8}, result={"status": "OK", "n": 8}, release=False))
            before = S.job_state(self.connect, "existing", "V1")
            self.assertIsNone(S.claim_job(self.connect, "existing", "V1"))
            self.assertFalse(S.checkpoint_job(self.connect, old, status="OK",
                cursor={"after": 999}, result={"status": "OK", "n": 999}))
            self.assertEqual(S.job_state(self.connect, "existing", "V1"), before)
        # Snapshot publication retains its global and row/fence checks.
        self.assertFalse(S.publish_snapshot(self.connect, "model", "V1", {"n": 999}, lease=old))
        self.assertEqual(S.load_snapshot(self.connect, "model", "V1"), original_model)
        self.assertTrue(S.publish_snapshot(self.connect, "model", "V1", {"n": 8}, lease=lease))
        self.assertEqual(S.load_snapshot(self.connect, "model", "V1")["payload"], {"n": 8})

    def test_creation_rechecks_existing_job_after_last_capacity_slot_race(self):
        selected, resume = threading.Event(), threading.Event()
        @contextmanager
        def paused_connect():
            with self.connect() as raw:
                class Gate:
                    paused = False
                    def transaction(self):
                        return raw.transaction()
                    def execute(self, sql, parameters=None):
                        result = raw.execute(sql, parameters) if parameters is not None else raw.execute(sql)
                        # Pause after the genuine initial missing-name SELECT,
                        # before this claimant can acquire the creation lock.
                        if (not self.paused and sql.lstrip().upper().startswith("SELECT")
                                and "veritas_learning_jobs" in sql):
                            self.paused = True
                            selected.set()
                            if not resume.wait(5):
                                raise RuntimeError("test creation barrier timed out")
                        return result
                yield Gate()
        with patch.object(S, "MAX_JOBS", 1):
            thread, values, errors = self._thread_call(
                lambda: S.claim_job(paused_connect, "last-slot", "V1", owner="later-worker"))
            try:
                self.assertTrue(selected.wait(3))
                winner = S.claim_job(self.connect, "last-slot", "V1", owner="first-worker")
                self.assertIsNotNone(winner)
                self.assertTrue(S.checkpoint_job(self.connect, winner, status="OK",
                    cursor={"after": 11}, result={"status": "OK", "n": 11}))
            finally:
                resume.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(values), 1)
            recovered = values[0]
            self.assertIsNotNone(recovered)
            self.assertGreater(recovered["fence"], winner["fence"])
            self.assertEqual(recovered["cursor"], {"after": 11})
            self.assertEqual(recovered["last_good"], {"status": "OK", "n": 11})
            before = S.job_state(self.connect, "last-slot", "V1")
            with self.assertRaises(ValueError):
                S.claim_job(self.connect, "overflow", "V1")
            self.assertEqual(S.job_state(self.connect, "last-slot", "V1"), before)
            with self.connect() as c:
                self.assertEqual(c.execute("SELECT count(*) n FROM veritas_learning_jobs").fetchone()["n"], 1)
            self.assertFalse(S.checkpoint_job(self.connect, winner, status="OK",
                cursor={"after": 999}, result={"n": 999}))
            self.assertTrue(S.checkpoint_job(self.connect, recovered, status="OK",
                cursor={"after": 12}, result={"status": "OK", "n": 12}))

    def _attempt_behind_expiring_job_row(self, lease, operation):
        with self.connect() as c:
            expires = c.execute("""UPDATE veritas_learning_jobs
                SET lease_until=clock_timestamp()+interval '3 seconds'
                WHERE name=%s RETURNING lease_until""", (lease["name"],)).fetchone()["lease_until"]
        before = S.job_state(self.connect, lease["name"], lease["version"])
        backend = []
        @contextmanager
        def tracked_connect():
            with self.connect() as c:
                backend.append(c.execute("SELECT pg_backend_pid() pid").fetchone()["pid"])
                yield c
        @contextmanager
        def long_test_transaction(connect):
            # Test-only allowance avoids a fragile sub-250ms scheduling race.
            with connect() as c, c.transaction():
                c.execute("SET LOCAL statement_timeout='10s'")
                c.execute("SET LOCAL lock_timeout='10s'")
                yield c
        thread = None
        with patch.object(S, "_transaction", long_test_transaction):
            try:
                with self.connect() as holder, holder.transaction():
                    # Expiry was committed above. This holder never changes the
                    # tuple, so release cannot induce an EPQ expiry recheck.
                    holder.execute("SELECT name FROM veritas_learning_jobs WHERE name=%s FOR UPDATE", (lease["name"],))
                    thread, values, errors = self._thread_call(lambda: operation(tracked_connect))
                    blocked = False
                    deadline = time.monotonic()+2
                    with self.connect() as observer:
                        while time.monotonic() < deadline:
                            if backend:
                                state = observer.execute("SELECT wait_event_type,wait_event FROM pg_stat_activity WHERE pid=%s", (backend[0],)).fetchone()
                                if state and state["wait_event_type"] == "Lock" and state["wait_event"] in ("transactionid", "tuple"):
                                    blocked = True
                                    break
                            time.sleep(.01)
                    self.assertTrue(blocked, "operation never waited on the real job row")
                    self.assertTrue(holder.execute("SELECT clock_timestamp()<%s alive", (expires,)).fetchone()["alive"])
                    holder.execute("SELECT pg_sleep(GREATEST(0,EXTRACT(epoch FROM (%s-clock_timestamp())))+.05)", (expires,))
            finally:
                # Holder contexts have exited before join, also on assertions.
                if thread is not None:
                    thread.join(5)
        self.assertFalse(thread.is_alive())
        return before, values, errors

    def test_checkpoint_cannot_advance_lease_expiring_behind_unchanged_row_lock(self):
        first = S.claim_job(self.connect, "expiry", "V1")
        self.assertTrue(S.checkpoint_job(self.connect, first, status="OK",
            cursor={"after": 3}, result={"status": "OK", "n": 3}))
        lease = S.claim_job(self.connect, "expiry", "V1")
        before, values, errors = self._attempt_behind_expiring_job_row(lease,
            lambda connect: S.checkpoint_job(connect, lease, status="OK",
                cursor={"after": 999}, result={"status": "OK", "n": 999}))
        self.assertEqual(errors, [])
        self.assertEqual(values, [False])
        self.assertEqual(S.job_state(self.connect, "expiry", "V1"), before)
        recovered = S.claim_job(self.connect, "expiry", "V1", owner="recovery-worker")
        self.assertIsNotNone(recovered)
        self.assertGreater(recovered["fence"], lease["fence"])
        self.assertEqual(recovered["cursor"], {"after": 3})
        self.assertEqual(recovered["last_good"], {"status": "OK", "n": 3})
        self.assertTrue(S.checkpoint_job(self.connect, recovered, status="OK",
            cursor={"after": 4}, result={"status": "OK", "n": 4}))

    def test_expired_waiting_publisher_rolls_back_evidence_and_preserves_model(self):
        first = S.claim_job(self.connect, "publisher", "V1")
        self.assertTrue(S.checkpoint_job(self.connect, first, status="OK",
            cursor={"after": 3}, result={"status": "OK", "n": 3}))
        lease = S.claim_job(self.connect, "publisher", "V1")
        self.assertTrue(S.publish_snapshot(self.connect, "expiry-model", "V1", {"n": 3}))
        original = S.load_snapshot(self.connect, "expiry-model", "V1")
        with self.connect() as c:
            c.execute("CREATE TABLE expiry_seen (id text PRIMARY KEY)")
        def attempt(connect):
            with S._transaction(connect) as c:
                c.execute("INSERT INTO expiry_seen VALUES ('synthetic-evidence')")
                if not S.publish_snapshot_in_transaction(c, "expiry-model", "V1", {"n": 999}, lease=lease):
                    raise RuntimeError("EXPECTED_EXPIRED_LEASE_REJECTION")
            return True
        before, values, errors = self._attempt_behind_expiring_job_row(lease, attempt)
        self.assertEqual(values, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(str(errors[0]), "EXPECTED_EXPIRED_LEASE_REJECTION")
        self.assertEqual(S.job_state(self.connect, "publisher", "V1"), before)
        self.assertEqual(S.load_snapshot(self.connect, "expiry-model", "V1"), original)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM expiry_seen").fetchone()["n"], 0)
        recovered = S.claim_job(self.connect, "publisher", "V1", owner="recovery-worker")
        self.assertIsNotNone(recovered)
        self.assertGreater(recovered["fence"], lease["fence"])
        self.assertEqual(recovered["cursor"], {"after": 3})
        self.assertEqual(recovered["last_good"], {"status": "OK", "n": 3})
        with S._transaction(self.connect) as c:
            c.execute("INSERT INTO expiry_seen VALUES ('synthetic-evidence')")
            self.assertTrue(S.publish_snapshot_in_transaction(c, "expiry-model", "V1", {"n": 4}, lease=recovered))
        self.assertEqual(S.load_snapshot(self.connect, "expiry-model", "V1")["payload"], {"n": 4})
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM expiry_seen").fetchone()["n"], 1)
