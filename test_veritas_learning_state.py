"""Cache commit semantics and durable job fencing; no production connection."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import veritas_learning_integrity as LI
import veritas_learning_exports as E
import veritas_learning_state as S


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


DSN=os.getenv("VERITAS_QUALITY_TEST_DSN","")
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
