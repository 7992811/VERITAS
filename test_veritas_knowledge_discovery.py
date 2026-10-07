"""Provider contracts and durable small-batch discovery; no live paid API calls."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import Mock, patch

import veritas_knowledge_discovery as K
import test_veritas_learning_state as state_tests


ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2401.12345v1</id><title>Bitcoin momentum</title>
<summary>Abstract with actual evidence.</summary><published>2024-01-01T00:00:00Z</published>
<author><name>A Researcher</name></author></entry></feed>'''


def namespace(connect=lambda: None):
    return {"pg_connect": connect, "KNOWLEDGE_AUTOMATION": True,
            "DISCOVERY_QUERIES": ["cryptocurrency momentum return predictability", "gold volatility"],
            "KNOWLEDGE_LLM_ENABLED": True, "OPENAI_API_KEY": "test-key-never-a-real-credential",
            "compile_pending_candidates": Mock(return_value=(0, [], {"audited": 0, "compiled": 0})),
            "candidate_relevance": lambda x: 4., "emit": Mock()}


class Response:
    def __init__(self, body=b'{"results":[]}', status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
        self.closed = self.read = False
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.closed = True
    def iter_bytes(self, chunk_size):
        self.read = True
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset+chunk_size]


class Client:
    def __init__(self, response):
        self.response, self.requests = response, []
    def __call__(self, **kwargs):
        self.options = kwargs
        return self
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def stream(self, method, url, params):
        self.requests.append((method, url, params))
        return self.response


class FakeStore:
    def __init__(self):
        self.row, self.statuses, self.schema_calls = None, [], 0
        self.reject_commit = False
    def ensure_schema(self, connect):
        self.schema_calls += 1
    def job_state(self, *args):
        return deepcopy(self.row)
    def claim_job(self, *args, **kwargs):
        return dict(deepcopy(self.row or {}), name=K.JOB, version=K.VERSION, fence=1, owner="test")
    def checkpoint_job(self, connect, lease, *, status, cursor, result, **kwargs):
        if self.reject_commit:
            return False
        K.STORE._json(cursor, K.STORE.MAX_CURSOR_BYTES)
        last_good = (self.row or {}).get("last_good")
        if status == "OK":
            K.STORE._good(result)
            last_good = deepcopy(result)
        self.row = dict(cursor=deepcopy(cursor), result=deepcopy(result), last_good=last_good)
        self.statuses.append(status)
        return True


class ProviderTests(unittest.TestCase):
    def test_terms_are_not_one_long_quoted_phrase_and_broadening_is_bounded(self):
        text = "cryptocurrency momentum return predictability"
        query = K.term_query(text, "arXiv")
        self.assertEqual(query, 'all:"cryptocurrency" AND all:"momentum" AND all:"return" AND all:"predictability"')
        self.assertEqual(K.term_query(text, "OpenAlex", True), "cryptocurrency momentum return")
        self.assertNotIn('"'+text+'"', query)
        self.assertIn("технический", K.term_query("технический анализ", "arXiv"))
        with self.assertRaises(ValueError):
            K.term_query(" \" ", "arXiv")

    def test_atom_success_and_atom_error_are_distinct(self):
        rows, raw = K.parse_provider("arXiv", ATOM, "bitcoin momentum")
        self.assertEqual(raw, 1)
        self.assertEqual(rows[0]["authors"], "A Researcher")
        self.assertEqual(rows[0]["year"], 2024)
        error = ATOM.replace(b"Bitcoin momentum", b"Error").replace(b"/abs/2401.12345v1", b"/api/errors#bad_query")
        with self.assertRaisesRegex(K.ProviderFailure, "ATOM_ERROR"):
            K.parse_provider("arXiv", error, "x")
        for invalid in (b"not XML", b"<html/>", b'<!DOCTYPE feed []>'+ATOM):
            with self.assertRaises(K.ProviderFailure):
                K.parse_provider("arXiv", invalid, "x")

    def test_json_error_or_wrong_shape_cannot_be_an_empty_success(self):
        for body in (b'{"error":"quota exhausted"}', b'{"results":null}', b'[]', b'garbage'):
            with self.assertRaises(K.ProviderFailure):
                K.parse_provider("OpenAlex", body, "q")
        self.assertEqual(K.parse_provider("OpenAlex", b'{"results":[]}', "q"), ([], 0))

    def test_openalex_retractions_and_inverted_positions_are_bounded(self):
        body = json.dumps({"results": [
            {"title": "Retracted", "is_retracted": True},
            {"title": "Evidence", "id": "https://openalex.org/W123", "doi": "https://doi.org/10.1/test",
             "abstract_inverted_index": {"actual": [0], "evidence": [1], "attack": [10**12, -1]},
             "authorships": [], "topics": []},
        ]}).encode()
        rows, raw = K.parse_provider("OpenAlex", body, "q")
        self.assertEqual(raw, 2);self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["abstract"], "actual evidence")
        self.assertEqual(rows[0]["doi"], "10.1/test")
        self.assertEqual(rows[0]["source_url"], "https://doi.org/10.1/test")
        self.assertLess(len(json.dumps(rows)), 2000)

    def test_semantic_missing_abstract_is_metadata_not_hidden_zero(self):
        body = json.dumps({"data": [{"paperId": "P1", "title": "Bitcoin markets", "abstract": None}]}).encode()
        rows, raw = K.parse_provider("SemanticScholar", body, "q")
        self.assertEqual((len(rows), raw), (1, 1))
        self.assertEqual(rows[0]["abstract"], "")

    def test_http_429_is_reported_honors_retry_after_and_never_reads_error_body(self):
        response = Response(b"secret echoed error", 429, {"Retry-After": "3600"})
        client = Client(response)
        engine = K.KnowledgeDiscovery(namespace(), client_factory=client, clock=lambda: 10000.)
        state = {}
        result = engine._discover(state)
        self.assertEqual(result["status"], "DEFERRED_PROVIDER")
        self.assertEqual(result["reason"], "HTTP_429")
        self.assertEqual(state["providers"]["OpenAlex"]["next_run_at"], 13600.)
        self.assertFalse(response.read);self.assertTrue(response.closed)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(client.requests[0][2]["sort"], "relevance_score:desc")
        self.assertNotIn("has_doi:true", client.requests[0][2]["filter"])

    def test_response_limit_closes_stream_instead_of_allocating_archive(self):
        response = Response(b"x"*(K.MAX_RESPONSE_BYTES+1))
        engine = K.KnowledgeDiscovery(namespace(), client_factory=Client(response))
        with self.assertRaisesRegex(K.ProviderFailure, "RESPONSE_TOO_LARGE"):
            engine._request("OpenAlex", "bitcoin", 0, 8)
        self.assertTrue(response.closed)

    def test_empty_is_explicit_and_query_broadens_before_rotation(self):
        engine = K.KnowledgeDiscovery(namespace(), client_factory=Client(Response()), clock=lambda: 10000.)
        state = {}
        out = engine._discover(state)
        self.assertEqual(out["status"], "EMPTY")
        self.assertEqual(out["raw_count"], 0)
        self.assertTrue(state["providers"]["OpenAlex"]["broaden"])
        self.assertEqual(state["providers"]["OpenAlex"]["query_index"], 0)

    def test_empty_first_provider_does_not_prevent_trying_other_providers(self):
        engine = K.KnowledgeDiscovery(namespace(), clock=lambda: 10000.)
        state = {}
        engine._request = Mock(return_value=([], 0))
        for _ in range(3):
            engine._discover(state)
        self.assertEqual([c.args[0] for c in engine._request.call_args_list], list(K.PROVIDERS))

    def test_completed_arxiv_sweep_preserves_minimum_daily_query_cache(self):
        engine = K.KnowledgeDiscovery(namespace(), clock=lambda: 10000.)
        progress = dict(provider="arXiv", query_index=len(engine.queries)-1, offset=0,
                        broaden=True, round_started_at=10000.)
        engine._advance(progress, raw_count=0, limit=8)
        self.assertEqual(progress["query_index"], 0)
        self.assertEqual(progress["next_run_at"], 10000.+86400)

    def test_query_paging_continues_past_first_pages_on_next_sweep(self):
        engine = K.KnowledgeDiscovery(namespace(), clock=lambda: 10000.)
        engine.queries = ["bitcoin momentum"]
        progress = dict(provider="OpenAlex", query_index=0, offset=0, broaden=False,
                        round_started_at=10000.)
        for _ in range(3):
            engine._advance(progress, raw_count=8, limit=8)
        self.assertEqual(progress["query_index"], 0)
        self.assertEqual(progress["offset"], 24)
        self.assertEqual(progress["pages"]["0"], [24, False])
        self.assertEqual(progress["page_pass"], 0)
        self.assertGreater(progress["next_run_at"], 10000.)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.store, self.stamp = FakeStore(), 10000.
        for name in ("ensure_schema", "job_state", "claim_job", "checkpoint_job"):
            p = patch.object(K.STORE, name, getattr(self.store, name));p.start();self.addCleanup(p.stop)
        self.ns = namespace()
        self.engine = K.KnowledgeDiscovery(self.ns, clock=lambda: self.stamp)
        self.engine._screen = Mock(return_value={"status": "EMPTY", "reason": "NO_PENDING_SCREENING"})
        self.engine._request = Mock(return_value=([], 0))
        self.engine._compiler_pending = Mock(return_value=True)
    def tick(self):
        out = self.engine.tick()
        self.stamp += 60
        return out

    def test_no_queries_and_zero_all_providers_never_report_ok(self):
        self.engine.queries = []
        statuses = [self.tick()["status"] for _ in range(3)]
        self.assertEqual(statuses, ["ERROR", "EMPTY", "EMPTY"])
        self.assertEqual([s for s in self.store.statuses if s != "RUNNING"], statuses)
        self.assertIsNone(self.store.row["last_good"])
        self.assertEqual(self.ns["compile_pending_candidates"].call_args.kwargs, {"limit": 1})

    def test_provider_failure_cannot_starve_pending_compiler(self):
        self.engine._request.side_effect = K.ProviderFailure("OpenAlex", "HTTP_403")
        self.ns["compile_pending_candidates"].return_value = (2, [], {"audited": 1, "compiled": 1})
        outcomes = [self.tick() for _ in range(3)]
        self.assertEqual([x["stage"] for x in outcomes], list(K.STAGES))
        self.assertEqual(outcomes[0]["status"], "ERROR")
        self.assertEqual(outcomes[-1]["rules_imported"], 2)
        self.assertEqual(outcomes[-1]["status"], "OK")
        self.assertEqual(outcomes[-1]["providers"]["OpenAlex"]["reason"], "HTTP_403")

    def test_counters_cursor_cooldown_and_last_good_restore_after_restart(self):
        self.engine._discover = Mock(return_value={"status": "OK", "received": 3, "new": 2})
        before = self.tick()
        self.assertEqual(before["candidates_new"], 2)
        replacement = K.KnowledgeDiscovery(self.ns, clock=lambda: self.stamp)
        replacement._screen = Mock(return_value={"status": "EMPTY", "reason": "NO_PENDING_SCREENING"})
        after = replacement.tick()
        self.assertEqual(after["stage"], "screening")
        self.assertEqual(after["candidates_new"], 2)
        self.assertEqual(self.store.row["last_good"]["status"], "OK")
        self.assertEqual(self.store.schema_calls, 2)
        self.assertEqual(self.ns["knowledge_automation_state"]["candidates_seen"], 3)

    def test_repeated_ticks_are_coalesced_and_no_thread_is_created(self):
        with patch.object(K.threading, "Thread", side_effect=AssertionError("new thread")):
            self.engine.tick()
            result = self.engine.tick()
            self.assertEqual(result["tick_status"], "NOT_DUE")
            self.assertEqual(self.engine._request.call_count, 1)

    def test_failed_state_commit_cannot_publish_uncommitted_counters(self):
        self.store.reject_commit = True
        self.engine._discover = Mock(return_value={"status": "OK", "received": 3, "new": 2})
        out = self.tick()
        self.assertEqual(out["status"], "ERROR")
        self.assertEqual(out["candidates_new"], 0)
        self.assertIsNone(self.store.row)

    def test_compiler_error_is_not_ok_and_cannot_expose_key_or_response(self):
        self.ns["compile_pending_candidates"].return_value = (0, ["429 echoed secret-api-key"], {"audited": 0})
        self.tick();self.tick();out = self.tick()
        self.assertEqual(out["status"], "ERROR")
        self.assertEqual(out["reason"], "COMPILER_RATE_LIMIT")
        self.assertNotIn("secret-api-key", json.dumps(out))

    def test_empty_queue_never_reserves_quota_and_new_work_is_immediately_due(self):
        self.engine._compiler_pending.return_value = False
        self.tick();self.tick();empty = self.tick()
        self.assertEqual(empty["reason"], "NO_PENDING_COMPILATION")
        self.assertEqual(empty["status"], "EMPTY")
        self.assertNotIn("attempts", empty["compiler"])
        self.assertNotIn("last_attempt_epoch", empty["compiler"])
        self.assertNotIn("RUNNING", self.store.statuses)
        self.ns["compile_pending_candidates"].assert_not_called()
        last_result = empty["compiler"]["last_result"]
        self.assertFalse(last_result["attempted"])
        self.engine._compiler_pending.return_value = True
        self.ns["compile_pending_candidates"].return_value = (1, [], {"compiled": 1})
        discovery = self.tick()
        self.assertEqual(discovery["compiler"]["last_result"], last_result)
        self.tick();ready = self.tick()
        self.assertEqual(ready["status"], "OK")
        self.assertEqual(ready["compiler"]["attempts"], 1)
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)

    def test_legacy_empty_without_no_call_proof_keeps_its_reservation(self):
        self.engine._ready = True
        self.engine._state = {"stage_index": 2, "compiler": {
            "attempts": 1, "last_attempt_epoch": self.stamp-10,
            "budget_next_at": self.stamp+2690, "next_run_at": self.stamp+2690},
            "summary": {"status": "EMPTY", "stage": "compilation",
                        "last_batch": {"reason": "NO_PENDING_COMPILATION", "audited": 0, "compiled": 0}}}
        self.engine._compiler_pending.return_value = False
        result = self.tick()
        self.assertEqual(result["reason"], "COMPILER_BUDGET_SPACING")
        self.assertEqual(result["compiler"]["attempts"], 1)
        self.assertEqual(result["compiler"]["next_run_at"], self.stamp-60+2690)
        self.engine._compiler_pending.assert_not_called()
        self.ns["compile_pending_candidates"].assert_not_called()

    def test_pending_sql_failure_is_error_without_charging_or_calling_compiler(self):
        self.engine._compiler_pending.side_effect = TimeoutError("private SQL")
        self.tick();self.tick();failed = self.tick()
        self.assertEqual((failed["status"], failed["reason"]), ("ERROR", "TIMEOUTERROR"))
        self.assertNotIn("attempts", failed["compiler"])
        self.assertNotIn("RUNNING", self.store.statuses)
        self.ns["compile_pending_candidates"].assert_not_called()
        self.assertNotIn("private SQL", json.dumps(failed))

    def test_queue_disappearing_after_reservation_cannot_refund_the_attempt(self):
        self.engine._compiler_pending.return_value = True
        self.tick();self.tick();empty = self.tick()
        self.assertEqual(empty["reason"], "NO_PENDING_COMPILATION")
        self.assertEqual(empty["compiler"]["attempts"], 1)
        self.assertEqual(empty["compiler"]["budget_next_at"], self.stamp-60+900)
        self.tick();self.tick();blocked = self.tick()
        self.assertEqual(blocked["reason"], "COMPILER_BUDGET_SPACING")
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)

    def test_transient_compiler_retry_has_persisted_backoff_and_validated_identity(self):
        candidate = "OA_"+"a"*24
        self.ns["compile_pending_candidates"].return_value = (0, [candidate+": ReadTimeout: private details"], {"audited": 0})
        self.tick();self.tick();out = self.tick()
        compiler = self.store.row["cursor"]["compiler"]
        self.assertEqual(compiler["retry_ids"], [candidate])
        self.assertEqual(compiler["next_run_at"], self.stamp-60+900)
        self.tick();self.tick();blocked = self.tick()
        self.assertEqual(blocked["reason"], "COMPILER_COOLDOWN")
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)
        self.assertNotIn(candidate, json.dumps(out))
        self.stamp = compiler["next_run_at"]+180
        self.engine._recover_compiler_candidates = Mock()
        self.ns["compile_pending_candidates"].return_value = (1, [], {"audited": 1, "compiled": 1})
        self.tick();self.tick();good = self.tick()
        self.engine._recover_compiler_candidates.assert_called_once_with([candidate])
        self.assertEqual(good["rules_imported"], 1)
        self.assertEqual(good["compiler"]["status"], "OK")

    def test_sql_failure_is_not_empty_and_retry_stage_stays_fair(self):
        class QueryCanceled(Exception):
            pass
        self.engine._screen.side_effect = QueryCanceled("private SQL")
        self.tick();failed = self.tick();next_result = self.tick()
        self.assertEqual(failed["status"], "ERROR")
        self.assertEqual(failed["reason"], "QUERYCANCELED")
        self.assertEqual(next_result["stage"], "compilation")
        self.assertNotIn("private SQL", json.dumps(failed))

    def test_default_budget_spaces_compile_attempts_but_search_and_screen_continue(self):
        self.ns["compile_pending_candidates"].return_value = (1, [], {"compiled": 1})
        results = [self.tick() for _ in range(18)]
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 2)
        self.assertEqual(self.engine._request.call_count, 6)
        self.assertEqual(self.engine._screen.call_count, 6)
        compiler = results[-1]["compiler"]
        self.assertEqual(compiler["budget_interval_seconds"], 21600)
        self.assertEqual(compiler["budget_attempt_limit"], 24)
        self.assertEqual(compiler["attempt_spacing_seconds"], 900)
        self.assertEqual(compiler["attempts"], 2)
        self.assertTrue(any(r.get("reason") == "COMPILER_BUDGET_SPACING" for r in results))

    def test_configured_budget_and_raised_errors_consume_attempt_spacing(self):
        self.ns.update(KNOWLEDGE_DISCOVERY_INTERVAL=7200, KNOWLEDGE_COMPILE_LIMIT=4)
        self.ns["compile_pending_candidates"].side_effect = TimeoutError("secret response body")
        self.tick();self.tick();failed = self.tick()
        self.assertEqual(failed["status"], "ERROR")
        self.assertEqual(failed["reason"], "COMPILER_TIMEOUT")
        self.assertEqual(failed["compiler"]["attempt_spacing_seconds"], 1800)
        self.assertEqual(failed["compiler"]["next_run_at"], self.stamp-60+1800)
        for _ in range(6):
            self.tick()
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)
        self.assertNotIn("secret response", json.dumps(self.engine.status()))

    def test_reservation_survives_paid_call_followed_by_failed_final_checkpoint(self):
        def paid_call(**kwargs):
            self.store.reject_commit = True
            return 1, [], {"compiled": 1}
        self.ns["compile_pending_candidates"].side_effect = paid_call
        self.tick();self.tick();failed = self.tick()
        self.assertEqual(failed["status"], "ERROR")
        reserved = self.store.row["cursor"]["compiler"]
        self.assertEqual(reserved["attempts"], 1)
        self.assertEqual(reserved["budget_next_at"], self.stamp-60+900)
        self.store.reject_commit = False
        replacement = K.KnowledgeDiscovery(self.ns, clock=lambda: self.stamp)
        replacement._screen = Mock(return_value={"status": "EMPTY"})
        replacement._request = Mock(return_value=([], 0))
        self.engine = replacement
        self.tick();self.tick();blocked = self.tick()
        self.assertEqual(blocked["reason"], "COMPILER_BUDGET_SPACING")
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)

    def test_failed_reservation_never_starts_a_compiler_call(self):
        self.engine._ready = True
        self.engine._state = {"stage_index": 2}
        self.store.reject_commit = True
        result = self.tick()
        self.assertEqual(result["status"], "ERROR")
        self.ns["compile_pending_candidates"].assert_not_called()

    def test_more_restrictive_configuration_cannot_retry_earlier(self):
        self.tick();self.tick();self.tick()
        attempt = self.store.row["cursor"]["compiler"]["last_attempt_epoch"]
        self.ns["KNOWLEDGE_COMPILE_LIMIT"] = 6
        self.tick();self.tick();result = self.tick()
        self.assertEqual(result["compiler"]["next_run_at"], attempt+3600)
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)


@unittest.skipUnless(os.getenv("VERITAS_QUALITY_TEST_DSN"), "isolated PostgreSQL test database not configured")
class DiscoverySQLTests(unittest.TestCase):
    def setUp(self):
        self.harness = state_tests.DurableStateSQLTests()
        self.harness.setUp();self.addCleanup(self.harness.tearDown)
        self.connect = self.harness.connect
        with self.connect() as c:
            c.execute("""CREATE TABLE knowledge_candidates(
                candidate_id TEXT PRIMARY KEY,discovered_at timestamptz,query TEXT,title TEXT,
                authors TEXT,year INTEGER,doi TEXT,source_url TEXT,venue TEXT,cited_by_count INTEGER,
                abstract TEXT,metadata jsonb,status TEXT,processed_at timestamptz,error TEXT)""")
        self.ns = namespace(self.connect)
        self.engine = K.KnowledgeDiscovery(self.ns)

    def item(self):
        return K.parse_provider("arXiv", ATOM, "bitcoin momentum")[0][0]

    def test_dedup_enriches_metadata_only_without_reopening_compiled_candidate(self):
        item = self.item();empty = dict(item, abstract="")
        self.assertEqual(self.engine._store([empty]), 1)
        self.assertEqual(self.engine._store([item]), 0)
        with self.connect() as c:
            row = c.execute("SELECT status,abstract FROM knowledge_candidates").fetchone()
            self.assertEqual(row["status"], "ready_for_compilation")
            self.assertEqual(row["abstract"], item["abstract"])
            c.execute("UPDATE knowledge_candidates SET status='compiled_shadow'")
        self.engine._store([dict(item, abstract=item["abstract"]+" more proof")])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM knowledge_candidates").fetchone()["status"], "compiled_shadow")

    def test_screening_is_bounded_and_preserves_provider_metadata(self):
        for i in range(12):
            item = dict(self.item(), candidate_id="source_"+str(i), abstract="actual market evidence "*40)
            self.engine._store([item])
        out = self.engine._screen()
        self.assertEqual(out["screened_in"], 8)
        with self.connect() as c:
            statuses = {r["status"]: r["n"] for r in c.execute("SELECT status,count(*) n FROM knowledge_candidates GROUP BY status").fetchall()}
            self.assertEqual(statuses, {"screened_in": 8, "ready_for_compilation": 4})
            self.assertTrue(c.execute("SELECT metadata FROM knowledge_candidates WHERE status='screened_in' LIMIT 1").fetchone()["metadata"]["arxiv_id"])

    def test_real_job_restart_preserves_cursor_and_empty_does_not_replace_last_good(self):
        self.engine._discover = Mock(return_value={"status": "OK", "received": 2, "new": 1})
        first = self.engine.tick()
        self.assertEqual(first["candidates_new"], 1)
        with self.connect() as c:
            c.execute("UPDATE veritas_learning_jobs SET next_run_at=clock_timestamp()-interval '1 second'")
        replacement = K.KnowledgeDiscovery(self.ns)
        after = replacement.tick()
        self.assertEqual(after["stage"], "screening")
        self.assertEqual(after["status"], "EMPTY")
        self.assertEqual(after["candidates_new"], 1)
        row = K.STORE.job_state(self.connect, K.JOB, K.VERSION)
        self.assertEqual(row["last_good"]["status"], "OK")

    def test_transient_quarantine_recovery_never_reopens_audit_rejection(self):
        item = self.item()
        self.engine._store([item])
        with self.connect() as c:
            c.execute("UPDATE knowledge_candidates SET status='quarantined',metadata=metadata || %s::jsonb",
                      (json.dumps({"compile_attempts": 3, "last_compile_error": "ReadTimeout"}),))
        self.engine._recover_compiler_candidates([item["candidate_id"]])
        with self.connect() as c:
            row = c.execute("SELECT status,metadata FROM knowledge_candidates").fetchone()
            self.assertEqual(row["status"], "screened_in")
            self.assertEqual(row["metadata"]["compile_attempts"], 0)
            c.execute("UPDATE knowledge_candidates SET status='llm_rejected'")
        self.engine._recover_compiler_candidates([item["candidate_id"]])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM knowledge_candidates").fetchone()["status"], "llm_rejected")

    def test_pending_probe_matches_compiler_status_and_empty_does_not_reserve(self):
        stamp = [10000.]
        self.engine = K.KnowledgeDiscovery(self.ns, clock=lambda: stamp[0])
        self.engine._ready = True
        self.engine._state = {"stage_index": 2}
        item = self.item()
        self.engine._store([item])
        for status in ("ready_for_compilation", "screened_out", "llm_rejected", "quarantined", "compiled_shadow"):
            with self.connect() as c:
                c.execute("UPDATE knowledge_candidates SET status=%s", (status,))
            self.assertFalse(self.engine._compiler_pending(), status)
        result = self.engine.tick()
        self.assertEqual(result["reason"], "NO_PENDING_COMPILATION")
        saved = K.STORE.job_state(self.connect, K.JOB, K.VERSION)
        self.assertNotIn("attempts", saved["cursor"]["compiler"])
        self.ns["compile_pending_candidates"].assert_not_called()
        with self.connect() as c:
            c.execute("UPDATE knowledge_candidates SET status='screened_in'")
            c.execute("""UPDATE veritas_learning_jobs SET next_run_at=clock_timestamp()-interval '1 second',
                cursor=jsonb_set(cursor,'{stage_index}','2'::jsonb) WHERE name=%s""", (K.JOB,))
        self.assertTrue(self.engine._compiler_pending())
        stamp[0] += 60
        self.ns["compile_pending_candidates"].return_value = (1, [], {"compiled": 1})
        replacement = K.KnowledgeDiscovery(self.ns, clock=lambda: stamp[0])
        success = replacement.tick()
        self.assertEqual(success["status"], "OK")
        self.assertEqual(success["compiler"]["attempts"], 1)
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)

    def test_paid_attempt_reservation_survives_process_loss_on_real_postgres(self):
        class ProcessLost(BaseException):
            pass
        stamp = [10000.]
        self.engine = K.KnowledgeDiscovery(self.ns, clock=lambda: stamp[0])
        self.engine._ready = True
        self.engine._state = {"stage_index": 2}
        self.engine._store([self.item()])
        with self.connect() as c:
            c.execute("UPDATE knowledge_candidates SET status='screened_in'")
        def paid_call(**kwargs):
            # This connection observes the committed reservation BEFORE the
            # simulated external call returns or its worker can checkpoint.
            row = K.STORE.job_state(self.connect, K.JOB, K.VERSION)
            self.assertEqual(row["cursor"]["compiler"]["attempts"], 1)
            self.assertEqual(row["cursor"]["compiler"]["budget_next_at"], 10900.)
            raise ProcessLost()
        self.ns["compile_pending_candidates"].side_effect = paid_call
        with self.assertRaises(ProcessLost):
            self.engine.tick()
        with self.connect() as c:
            c.execute("""UPDATE veritas_learning_jobs SET
                lease_until=clock_timestamp()-interval '1 second',
                next_run_at=clock_timestamp()-interval '1 second',
                cursor=jsonb_set(cursor,'{stage_index}','2'::jsonb)
                WHERE name=%s""", (K.JOB,))
        stamp[0] += 300
        replacement = K.KnowledgeDiscovery(self.ns, clock=lambda: stamp[0])
        result = replacement.tick()
        self.assertEqual(result["status"], "DEFERRED_COMPILER")
        self.assertEqual(result["reason"], "COMPILER_BUDGET_SPACING")
        self.assertEqual(self.ns["compile_pending_candidates"].call_count, 1)


if __name__ == "__main__":
    unittest.main()
