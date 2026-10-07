"""Cache commit semantics and durable job fencing; no production connection."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import threading
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
