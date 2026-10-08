"""Prospective knowledge contract and isolated PostgreSQL commit tests."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import veritas_knowledge_validation as K
import veritas_learning_state as STORE

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SOURCE = {"venue": "TEST", "symbol": "BTCUSD", "instrument_type": "SPOT"}
AUDIT = dict(decision="USE", claim="Testable prospective trend hypothesis", evidence_strength="MEDIUM",
             supported_fields=["trend_score"], rationale="The supplied abstract defines the trend field.")
ACADEMIC = dict(source_id="AUTO_TEST", title="Prospective trend paper", authors="A Researcher", year=2025,
                source_type="machine_extracted_academic", url="https://example.org/paper", claim="Trend hypothesis", evidence_grade="UNVERIFIED")
RULE = dict(rule_id="AUTO_RULE", source_id="AUTO_TEST", agent="trend", asset_scope=["BTC"], horizons=["5m"],
            action="LONG", status="shadow", conditions=[dict(field="trend_score", op="gt", value=0)],
            prior_weight=.01, hypothesis="Positive trend predicts a rise", mechanism="Persistence", formalization_note="Abstract extracted")

# The v91.8.10 rule-page reader, before bounding its source/audit joins.
LEGACY_CATALOG_RULES_SQL = """WITH page AS MATERIALIZED (
        SELECT rule_id,source_id,agent,asset_scope,horizons,action,status,
          CASE WHEN octet_length(conditions::text)<=4096 THEN conditions ELSE NULL END conditions,
          prior_weight,CASE WHEN octet_length(hypothesis)<=2048 THEN hypothesis ELSE NULL END hypothesis,
          CASE WHEN octet_length(mechanism)<=2048 THEN mechanism ELSE NULL END mechanism,
          CASE WHEN octet_length(formalization_note)<=2048 THEN formalization_note ELSE NULL END formalization_note
        FROM knowledge_rules WHERE rule_id>%s AND (left(source_id,5)='AUTO_'
          OR EXISTS (SELECT 1 FROM knowledge_validation_rules known
            WHERE known.rule_id=knowledge_rules.rule_id AND known.active))
        ORDER BY rule_id LIMIT %s)
        SELECT p.*,jsonb_build_object('source_id',s.source_id,'title',left(s.title,512),
          'authors',left(s.authors,512),'year',s.year,'source_type',s.source_type,'url',left(s.url,1024),
          'claim',left(s.claim,1024),'evidence_grade',s.evidence_grade) AS source,
          a.audit,a.checked_at audit_checked_at,a.revision audit_revision
        FROM page p LEFT JOIN knowledge_sources s ON s.source_id=p.source_id
        LEFT JOIN knowledge_validation_source_audits a ON a.source_id=p.source_id
        ORDER BY p.rule_id"""


def registration(at=T0):
    return K._registration(RULE, ACADEMIC, AUDIT, at)


def match_for(entry):
    return {k: deepcopy(entry[k]) for k in K._MATCH_FIELDS}


def scope():
    return K._scope("BTC", "5m", "TREND", "policy-before-outcome", SOURCE)


def stamp_for(candidate, decision, *, direction="LONG", phase=None):
    fields = (*K._CONTRACT_FIELDS, "contract_hash", "trial_id")
    stamp = {k: deepcopy(candidate[k]) for k in fields if k != "criteria"}
    stamp.update(decision_at=decision.isoformat(), base_direction=direction,
                 phase=phase or ("OOS" if candidate.get("evaluation_id") else "TRAIN"),
                 evaluation_id=candidate.get("evaluation_id"),
                 size_execution=K.size_execution(candidate["action"], direction, fraction=.5, position_step=.05, max_fraction=1.))
    return stamp


def observation(candidate, index, decision, *, direction="LONG", trade=False, phase=None):
    stamp = stamp_for(candidate, decision, direction=direction, phase=phase)
    row = dict(asset="BTC", horizon="5m", regime="TREND", policy_hash=scope()["policy_hash"], source_identity=SOURCE,
               idea_id="idea-"+str(index), episode_key="receipt-"+str(index), direction=direction,
               known_at=(decision-timedelta(seconds=1)).isoformat(), decision_at=decision.isoformat(),
               outcome_at=(decision+timedelta(minutes=5)).isoformat(), observed_at=(decision+timedelta(minutes=6)).isoformat(),
               evidence_hash="evidence-"+str(index)+(":trade" if trade else ":direction"), evidence_version="TEST_ORIGINAL_V1",
               source_verified=True, evidence_valid=True, independence_verified=True,
               knowledge_trials=[stamp], forward_return=.01)
    if trade:
        base = .3 if direction == "LONG" else -.1
        row.update(proof_kind="SIMULATED_SIZE_ON_OBSERVED_PATH", baseline_net_r=base,
                   costs_verified=True, risk_verified=True,
                   knowledge_cashflows=dict(proof_kind=K.PROOF_KIND, cashflows_verified=True,
                       source_evidence_hash=row["evidence_hash"], baseline_net_r=base, baseline_risk_r=1., risk_cap_r=1.2,
                       fraction_before=.5,position_step=.05,max_fraction=1.))
    return row, stamp


def fresh_candidate():
    return K._new_trial(K._template(match_for(registration()), scope()))


def trained():
    candidate = fresh_candidate()
    for i in range(K.MIN_TRAIN):
        row, stamp = observation(candidate, i, T0+timedelta(hours=i+1))
        candidate, reason = K.evaluate(candidate, row, stamp, now=row["observed_at"])
        if reason:
            raise AssertionError(reason)
    return candidate


def proven():
    candidate = trained(); start = K._time(candidate["oos_registered_at"])+timedelta(days=1)
    for i in range(100):
        for trade in (False, True):
            row, stamp = observation(candidate, "oos-"+str(i), start+timedelta(hours=12*i),
                                     direction="LONG" if i % 2 == 0 else "SHORT", trade=trade)
            candidate, reason = K.evaluate(candidate, row, stamp, now=row["observed_at"])
            if reason:
                raise AssertionError(reason)
    return candidate, K._time(row["observed_at"])


class KnowledgeContractTests(unittest.TestCase):
    def setUp(self):
        self.saved = (deepcopy(K._RULES), deepcopy(K._TRIALS), deepcopy(K._BOARD), K._VERIFIED_EPOCH)
        K._RULES.clear(); K._TRIALS.clear(); K._BOARD = {}; K._VERIFIED_EPOCH = None

    def tearDown(self):
        K._RULES, K._TRIALS, K._BOARD, K._VERIFIED_EPOCH = self.saved

    def test_capture_requires_original_supported_audit_and_durable_registration(self):
        self.assertIsNone(K.capture(RULE, ACADEMIC, audit=AUDIT, now=T0))
        entry = registration()
        K._RULES[entry["rule_key"]] = entry
        self.assertEqual(K.capture(RULE, now=T0), match_for(entry))
        bad = deepcopy(RULE); bad["conditions"][0]["field"] = "invented_field"
        self.assertIsNone(K.capture(bad, now=T0))
        with self.assertRaises(ValueError):
            K._registration(bad, ACADEMIC, AUDIT, T0)
        with self.assertRaises(ValueError):
            K._registration(RULE, ACADEMIC, None, T0)
        changed = dict(AUDIT, decision="REJECT")
        self.assertIsNone(K.capture(RULE, audit=changed, now=T0))
        self.assertIsNone(K.capture(RULE, now=T0+timedelta(seconds=K.CATALOG_TTL+1)))

    def test_source_audit_and_definition_mutations_never_reuse_identity(self):
        original = registration()
        changed = K._registration(dict(RULE, prior_weight=.02), ACADEMIC, AUDIT, T0)
        self.assertNotEqual(original["rule_key"], changed["rule_key"])
        changed = K._registration(RULE, ACADEMIC, dict(AUDIT, claim="Different claim"), T0)
        self.assertNotEqual(original["audit_hash"], changed["audit_hash"])
        original["definition"]["action"] = "SHORT"
        self.assertFalse(K._rule_valid(original, T0))

    def test_frozen_quantization_has_no_large_or_aligned_reduction(self):
        self.assertEqual(K.size_execution("LONG", "SHORT", fraction=.1, position_step=.05, max_fraction=1.)["effective_multiplier"], 1.)
        self.assertEqual(K.size_execution("LONG", "LONG", fraction=.56, position_step=.05, max_fraction=1.)["fraction_after"], .56)
        self.assertAlmostEqual(K.size_execution("LONG", "SHORT", fraction=.5, position_step=.05, max_fraction=1.)["effective_multiplier"], .9)
        self.assertIsNone(K.size_execution("LONG", "SHORT", fraction=.5, position_step=0, max_fraction=1.))

    def test_capture_adjust_freezes_trial_without_probability_or_authority(self):
        entry = registration(); K._RULES[entry["rule_key"]] = entry
        answer = K.adjust([match_for(entry)], asset="BTC", horizon="5m", regime="TREND", policy_hash=scope()["policy_hash"],
                          source_identity=SOURCE, base_direction="SHORT", fraction=.5, position_step=.05, max_fraction=1.,
                          now=T0+timedelta(seconds=10))
        self.assertEqual(answer["size_multiplier"], 1.)
        self.assertEqual(answer["contribution"], 0.)
        self.assertEqual(len(answer["trials"]), 1)
        self.assertLessEqual(len(STORE._json(answer["trials"][0], 2048).encode()), 2048)
        self.assertAlmostEqual(answer["trials"][0]["size_execution"]["effective_multiplier"], .9)
        self.assertEqual(K._new_trial(answer["trials"][0])["state"], "TRAINING")

    def test_train_eighty_then_only_future_sealed_disjoint_oos(self):
        candidate = trained()
        self.assertEqual(candidate["training"]["n"], 80)
        self.assertEqual(candidate["state"], "OOS")
        self.assertEqual(candidate["direction"]["n"], 0)
        earlier = K._time(candidate["oos_registered_at"])-timedelta(minutes=20)
        row, stamp = observation(candidate, "old", earlier)
        result, reason = K.evaluate(candidate, row, stamp, now=K._time(candidate["oos_registered_at"])+timedelta(hours=1))
        self.assertEqual(reason, "OOS_NOT_PREREGISTERED_BEFORE_DECISION")
        self.assertEqual(result["direction"]["n"], 0)
        row, stamp = observation(candidate, "new", earlier+timedelta(days=1), phase="TRAIN")
        self.assertEqual(K.evaluate(candidate, row, stamp, now=row["observed_at"])[1], "OOS_NOT_PREREGISTERED_BEFORE_DECISION")

    def test_no_direction_or_size_authority_at_99_oos_and_no_fabricated_profit(self):
        candidate = trained(); start = K._time(candidate["oos_registered_at"])+timedelta(days=1)
        for i in range(100):
            decision = start+timedelta(hours=12*i)
            row, stamp = observation(candidate, "oos-"+str(i), decision)
            candidate, reason = K.evaluate(candidate, row, stamp, now=row["observed_at"])
            self.assertIsNone(reason)
            if i == 98:
                self.assertFalse(candidate["direction_pass"])
        self.assertTrue(candidate["direction_pass"])
        self.assertFalse(candidate["net_pass"])
        self.assertEqual(candidate["state"], "OOS")
        self.assertIsNone(candidate["valid_until"])

    def test_promotion_uses_all_aligned_and_opposed_net_opportunities(self):
        candidate = trained(); start = K._time(candidate["oos_registered_at"])+timedelta(days=1)
        for i in range(100):
            direction = "LONG" if i % 2 == 0 else "SHORT"
            for trade in (False, True):
                row, stamp = observation(candidate, "oos-"+str(i), start+timedelta(hours=12*i), direction=direction, trade=trade)
                candidate, reason = K.evaluate(candidate, row, stamp, now=row["observed_at"])
                self.assertIsNone(reason)
            if i < 99:
                self.assertNotEqual(candidate["state"], "PROMOTED")
        self.assertEqual(candidate["state"], "PROMOTED")
        self.assertEqual(candidate["net"]["n"], 100)
        self.assertAlmostEqual(candidate["net"]["delta_sum"], .5)
        self.assertGreater(K._summary(candidate["net"])["mean_lower"], 0)
        self.assertGreater(K._summary(candidate["net"])["delta_lower"], 0)
        self.assertFalse("profitability_proven" in candidate)

    def test_matched_only_losing_base_trades_cannot_claim_positive_net_profit(self):
        candidate = trained(); start = K._time(candidate["oos_registered_at"])+timedelta(days=1)
        for i in range(100):
            for trade in (False, True):
                row, stamp = observation(candidate, i, start+timedelta(days=i), direction="SHORT", trade=trade)
                candidate, _ = K.evaluate(candidate, row, stamp, now=row["observed_at"])
        self.assertTrue(candidate["direction_pass"])
        self.assertFalse(candidate["net_pass"])
        self.assertEqual(candidate["state"], "OOS")

    def test_net_cost_scope_and_contract_exclusions_are_explicit(self):
        candidate = trained(); at = K._time(candidate["oos_registered_at"])+timedelta(days=1)
        row, stamp = observation(candidate, 1, at, trade=True)
        for field, value, expected in (("knowledge_cashflows", None, "MISSING_PAIRED_KNOWLEDGE_CASHFLOWS"),
                ("costs_verified", False, "UNVERIFIED_NET_COST_OR_RISK"),
                ("source_verified", False, "UNVERIFIED_OUTCOME"),
                ("policy_hash", "changed", "SOURCE_OR_POLICY_SCOPE_MISMATCH")):
            with self.subTest(field=field):
                result, reason = K.evaluate(candidate, dict(row, **{field: value}), stamp, now=row["observed_at"])
                self.assertEqual(reason, expected)
                self.assertEqual(result["net"]["n"], 0)
        altered = dict(stamp, action="SHORT")
        self.assertEqual(K.evaluate(candidate, row, altered, now=row["observed_at"])[1], "IMMUTABLE_CONTRACT_MISMATCH")
        mutated = deepcopy(candidate); mutated["criteria"]["train"] = 1
        self.assertEqual(K.evaluate(mutated, row, stamp, now=row["observed_at"])[0]["state"], "REVOKED")
        for key in ("fraction_before", "position_step", "max_fraction"):
            modified = deepcopy(row); modified["knowledge_cashflows"][key] *= 2
            self.assertEqual(K.evaluate(candidate, modified, stamp, now=row["observed_at"])[1],
                             "OBSERVED_EXPOSURE_DOES_NOT_MATCH_DECLARED_TRIAL")

    def test_family_budget_is_frozen_and_covers_all_fixed_looks(self):
        self.assertAlmostEqual(K.CRITERIA["per_test_alpha"]*K.MAX_FAMILY*len(K.LOOKS)*3, .05)
        candidate = fresh_candidate()
        self.assertEqual(candidate["criteria"], K.CRITERIA)
        self.assertEqual(candidate["trial_id"], candidate["contract_hash"])

    def test_integrity_permit_is_process_bound_and_immediately_revoked(self):
        integrity = dict(process_epoch="test-process", revocation_generation=2, generation=9, ready=True)
        status = dict(evidence_revalidation_pending=False, verified_process_epoch="test-process", verified_revocation_generation=2)
        with patch.object(K.LI, "memory_state", return_value=integrity), patch.object(K.EXPORTS, "memory_current", return_value=True):
            self.assertFalse(K._integrity_current())
            self.assertTrue(K.update_integrity_verification(status))
            self.assertTrue(K._integrity_current())
            integrity["revocation_generation"] = 3
            self.assertFalse(K._integrity_current())
            self.assertFalse(K.update_integrity_verification(status))

    def test_batch_limits_reject_instead_of_silently_truncating(self):
        with self.assertRaises(ValueError):
            K.run_batch(None, [{}]*33, now=T0)
        with self.assertRaises(ValueError):
            K._definition(dict(RULE, hypothesis="x"*9000))

    def test_trial_window_rejects_invalid_shape_or_unselected_stamp_before_sql(self):
        candidate = fresh_candidate()
        row, _ = observation(candidate, "window", T0+timedelta(minutes=10))
        valid = deepcopy(row["knowledge_trials"][0])
        connect = Mock(side_effect=AssertionError("invalid window reached SQL"))
        for window in ((-1, 0), (0, 0), (0, 2), (1, 2), (True, 1), (0, True),
                       (0., 1), ("0", 1), (0,), (0, 1, 2), "01", iter([0, 1])):
            with self.subTest(window=str(window)), self.assertRaises(ValueError):
                K.run_batch(connect, [row], now=row["observed_at"], trial_window=window)
        for rows in ([], [row, row], [None]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                K.run_batch(connect, rows, now=row["observed_at"], trial_window=(0, 1))
        for stamps in (None, [], {}, "", (valid,), [valid]*9, [valid, None], [valid, []],
                       [valid, {"oversized": "x"*2049}], [valid, {"invalid": float("nan")}],
                       [valid, {"invalid": object()}]):
            with self.subTest(stamps=str(stamps)[:80]), self.assertRaises(ValueError):
                K.run_batch(connect, [dict(row, knowledge_trials=stamps)],
                            now=row["observed_at"], trial_window=(0, 1))
        connect.assert_not_called()

    def test_trial_window_passes_original_raw_and_stamp_to_unchanged_evaluator(self):
        entry = registration()
        candidate = fresh_candidate()
        row, stamp = observation(candidate, "window", T0+timedelta(minutes=10))
        row["knowledge_trials"] = [deepcopy(stamp), stamp, deepcopy(stamp)]
        original = deepcopy(row)
        class Connection:
            def execute(self, sql, args=None):
                record = None
                if "SELECT registered" in sql:
                    record = {"registered": 0}
                elif "SELECT payload,active,checked_at" in sql:
                    record = {"payload": entry, "active": True, "checked_at": T0}
                return SimpleNamespace(fetchone=lambda: record, fetchall=lambda: [])
        with patch.object(K, "_transaction", return_value=nullcontext(Connection())), \
             patch.object(STORE, "load_snapshot_in_transaction", return_value=None), \
             patch.object(STORE, "publish_snapshot_in_transaction", return_value=True), \
             patch.object(K, "evaluate", wraps=K.evaluate) as evaluate:
            result = K.run_batch(None, [row], now=row["observed_at"], trial_window=(1, 2))
        evaluate.assert_called_once()
        self.assertIs(evaluate.call_args.args[1], row)
        self.assertIs(evaluate.call_args.args[2], stamp)
        self.assertEqual(row, original)
        self.assertEqual(result["counts"]["direction"], 1)

    def test_rule_page_uses_audit_pk_join_without_archive_hash_scan(self):
        source = inspect.getsource(K._catalog_rules)
        self.assertNotIn("knowledge_candidates", source)
        self.assertNotIn("sha256", source)
        self.assertIn("a.source_id=p.source_id", source)
        self.assertEqual(K.CATALOG_RULE_BATCH, 8)
        self.assertLessEqual(K.CATALOG_AUDIT_BATCH, 32)

    def test_withdrawn_then_restored_identical_audit_requires_new_proof_identity(self):
        one = K._registration(RULE, ACADEMIC, AUDIT, T0, audit_revision=1)
        three = K._registration(RULE, ACADEMIC, AUDIT, T0, audit_revision=3)
        self.assertEqual(one["audit_hash"], three["audit_hash"])
        self.assertNotEqual(one["rule_key"], three["rule_key"])
        self.assertTrue(K._rule_valid(three, T0))

    def test_proven_advice_is_minimum_size_only_and_expiry_is_immediate(self):
        candidate, at = proven()
        entry = registration(); entry["checked_at"] = at.isoformat()
        K._RULES[entry["rule_key"]] = entry
        K._TRIALS[K._base_key(entry["rule_key"], scope())] = K._hot_trial(candidate)
        kwargs = dict(asset="BTC", horizon="5m", regime="TREND", policy_hash=scope()["policy_hash"], source_identity=SOURCE,
                      base_direction="SHORT", fraction=.5, position_step=.05, max_fraction=1.)
        with patch.object(K, "_integrity_current", return_value=True):
            applied = K.adjust([match_for(entry)], now=at, **kwargs)
            self.assertAlmostEqual(applied["size_multiplier"], .9)
            self.assertEqual(applied["contribution"], 0.)
            self.assertEqual(K.adjust([match_for(entry)], now=at, **dict(kwargs, base_direction="LONG"))["size_multiplier"], 1.)
            expired_at = at+timedelta(days=K.VALID_DAYS+1)
            entry["checked_at"] = expired_at.isoformat()
            self.assertEqual(K.adjust([match_for(entry)], now=expired_at, **kwargs)["size_multiplier"], 1.)
            row, stamp = observation(candidate, "expired", expired_at)
            self.assertEqual(K.evaluate(candidate, row, stamp, now=row["observed_at"])[0]["state"], "EXPIRED")

    def test_worst_case_public_board_and_ram_contract_are_bounded(self):
        candidate, at = proven()
        # Even a historical store larger than the hot limit cannot double a
        # full set of summaries into the public profiles list.
        pool = []
        for i in range(128):
            item = deepcopy(candidate); item["rule_id"] = "AUTO_"+str(i)+"x"*64
            pool.append(item)
        board = K._materialize_board(dict(version=K.VERSION, counts={}, reasons={}), pool, at, 128, {})
        self.assertEqual(len(board["candidates"]), 32)
        self.assertEqual(len(board["profiles"]), 32)
        self.assertLess(len(STORE._json(board, K.MAX_BOARD_BYTES).encode()), K.MAX_BOARD_BYTES)
        compact = K._hot_trial(candidate)
        self.assertNotIn("training", compact); self.assertNotIn("net", compact)
        self.assertLess(len(STORE._json(compact, 4096).encode()), 4096)

    def test_pending_old_oos_outcome_cannot_renew_promoted_proof(self):
        candidate, at = proven()
        row, stamp = observation(candidate, "old-oos", at-timedelta(minutes=10), trade=True, phase="OOS")
        row.update(outcome_at=(at+timedelta(minutes=1)).isoformat(), observed_at=(at+timedelta(minutes=2)).isoformat())
        result, reason = K.evaluate(candidate, row, stamp, now=row["observed_at"])
        self.assertEqual(reason, "MONITOR_NOT_PREREGISTERED_BEFORE_DECISION")
        self.assertEqual(result["monitor_net"]["n"], 0)
        self.assertEqual(result["valid_until"], candidate["valid_until"])
        row, stamp = observation(candidate, "fresh-monitor", at+timedelta(minutes=1), trade=True, phase="MONITOR")
        result, reason = K.evaluate(candidate, row, stamp, now=row["observed_at"])
        self.assertIsNone(reason)
        self.assertEqual(result["monitor_net"]["n"], 1)


DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class KnowledgeValidationSQLTests(unittest.TestCase):
    def setUp(self):
        self.saved = (deepcopy(K._RULES), deepcopy(K._TRIALS), deepcopy(K._BOARD), K._VERIFIED_EPOCH)
        K._RULES.clear(); K._TRIALS.clear(); K._BOARD = {}; K._VERIFIED_EPOCH = None
        from test_veritas_learning_state import DurableStateSQLTests
        self.database = DurableStateSQLTests(); self.database.setUp()
        self.addCleanup(self.database.tearDown)
        self.connect = self.database.connect
        K.ensure_schema(self.connect)
        with self.connect() as c:
            c.execute("""CREATE TABLE knowledge_sources(source_id text PRIMARY KEY,title text,authors text,year integer,
                source_type text,url text,evidence_grade text,claim text)""")
            c.execute("""CREATE TABLE knowledge_rules(rule_id text PRIMARY KEY,source_id text,agent text,asset_scope jsonb,horizons jsonb,
                action text,status text,conditions jsonb,prior_weight double precision,hypothesis text,mechanism text,formalization_note text)""")
            c.execute("""CREATE TABLE knowledge_candidates(candidate_id text PRIMARY KEY,doi text,metadata jsonb,processed_at timestamptz)""")
        self.candidate_id = "candidate-original"
        self.source_id = "AUTO_"+hashlib.sha256(self.candidate_id.encode()).hexdigest()[:20].upper()
        self.rule = dict(RULE, source_id=self.source_id)
        with self.connect() as c:
            source = dict(ACADEMIC, source_id=self.source_id)
            c.execute("INSERT INTO knowledge_sources VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                      tuple(source[k] for k in ("source_id", "title", "authors", "year", "source_type", "url", "evidence_grade", "claim")))
            c.execute("INSERT INTO knowledge_candidates VALUES(%s,NULL,%s::jsonb,now())", (self.candidate_id, json.dumps(dict(llm_audit=AUDIT))))
            c.execute("INSERT INTO knowledge_rules VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s)",
                tuple(json.dumps(self.rule[k]) if k in ("asset_scope", "horizons", "conditions") else self.rule[k]
                      for k in ("rule_id", "source_id", "agent", "asset_scope", "horizons", "action", "status", "conditions", "prior_weight", "hypothesis", "mechanism", "formalization_note")))

    def tearDown(self):
        K._RULES, K._TRIALS, K._BOARD, K._VERIFIED_EPOCH = self.saved

    def _ready(self):
        result = K.refresh_catalog(self.connect, now=T0)
        self.assertEqual(result["phase"], "audit_index")
        result = K.refresh_catalog(self.connect, now=T0)
        self.assertEqual(result["audited"], 1)
        match = K.capture(self.rule, now=T0)
        self.assertIsNotNone(match)
        return K._new_trial(K._template(match, scope()))

    def _consumed_state(self):
        with self.connect() as c:
            return {
                "trials": c.execute("SELECT * FROM knowledge_validation_trials ORDER BY trial_id").fetchall(),
                "seen": c.execute("SELECT * FROM knowledge_validation_seen ORDER BY idea_key").fetchall(),
                "streams": c.execute("SELECT * FROM knowledge_validation_streams ORDER BY stream_key").fetchall(),
                "family": c.execute("SELECT registered FROM knowledge_validation_family").fetchone(),
                "board": c.execute("SELECT payload FROM veritas_learning_snapshots WHERE name=%s", (K.SNAPSHOT_NAME,)).fetchone(),
            }

    def _clear_consumed_state(self):
        # The inherited DSN guard and unique schema confine this reset to this
        # test's database fixture; audited source/rule registrations stay fixed.
        with self.connect() as c:
            c.execute("TRUNCATE knowledge_validation_trials,knowledge_validation_seen,knowledge_validation_streams")
            c.execute("UPDATE knowledge_validation_family SET registered=0")
            c.execute("DELETE FROM veritas_learning_snapshots WHERE name=%s", (K.SNAPSHOT_NAME,))
        K._TRIALS.clear(); K._BOARD = {}

    def test_sql_all_eight_trials_equal_sequential_windows_and_replays(self):
        rules = [self.rule]
        with self.connect() as c:
            for i in range(1, K.MAX_TRIALS):
                rule = dict(self.rule, rule_id="AUTO_RULE_"+str(i), action="SHORT" if i % 2 else "LONG")
                rules.append(rule)
                c.execute("""INSERT INTO knowledge_rules
                    SELECT %s,source_id,agent,asset_scope,horizons,%s,status,conditions,prior_weight,
                        hypothesis,mechanism,formalization_note FROM knowledge_rules WHERE rule_id=%s""",
                    (rule["rule_id"], rule["action"], self.rule["rule_id"]))
        K.refresh_catalog(self.connect, now=T0)
        self.assertEqual(K.refresh_catalog(self.connect, now=T0)["audited"], K.MAX_TRIALS)
        candidates = [K._new_trial(K._template(K.capture(rule, now=T0), scope())) for rule in rules]
        decision = T0+timedelta(minutes=10)
        row, _ = observation(candidates[0], "all-eight-original", decision)
        row["knowledge_trials"] = [stamp_for(candidate, decision) for candidate in candidates]
        immutable = deepcopy(row)
        whole = K.run_batch(self.connect, [row], now=row["observed_at"])
        expected = self._consumed_state()
        self.assertEqual(whole["counts"]["direction"], K.MAX_TRIALS)
        self.assertEqual(expected["family"]["registered"], K.MAX_TRIALS)
        self._clear_consumed_state()
        for i in range(K.MAX_TRIALS):
            answer = K.run_batch(self.connect, [row], now=row["observed_at"], trial_window=(i, i+1))
            self.assertEqual(answer["counts"]["direction"], i+1)
        self.assertEqual(answer, whole)
        self.assertEqual(self._consumed_state(), expected)
        # A lost caller checkpoint can repeat any committed window, or even
        # replay the old whole-row API, without inflating family or evidence.
        for i in reversed(range(K.MAX_TRIALS)):
            K.run_batch(self.connect, [row], now=row["observed_at"], trial_window=(i, i+1))
        K.run_batch(self.connect, [row], now=row["observed_at"])
        self.assertEqual(self._consumed_state(), expected)
        self.assertEqual(row, immutable)
        self.assertTrue(all(r["evidence_hash"] == row["evidence_hash"] for r in expected["seen"]))

    def test_sql_window_failure_rolls_back_and_unselected_bad_stamp_cannot_commit(self):
        candidate = self._ready()
        row, stamp = observation(candidate, "window-rollback", T0+timedelta(minutes=10))
        row["knowledge_trials"] = [stamp, deepcopy(stamp)]
        before = self._consumed_state()
        for bad in (None, {"too_big": "x"*2049}):
            malformed = dict(row, knowledge_trials=[stamp, bad])
            with self.assertRaises(ValueError):
                K.run_batch(self.connect, [malformed], now=row["observed_at"], trial_window=(0, 1))
            self.assertEqual(self._consumed_state(), before)
        with patch.object(STORE, "publish_snapshot_in_transaction", side_effect=RuntimeError("commit blocked")):
            with self.assertRaisesRegex(RuntimeError, "commit blocked"):
                K.run_batch(self.connect, [row], now=row["observed_at"], trial_window=(0, 1))
        self.assertEqual(self._consumed_state(), before)
        retried = K.run_batch(self.connect, [row], now=row["observed_at"], trial_window=(0, 1))
        self.assertEqual(retried["counts"]["direction"], 1)
        self.assertEqual(len(self._consumed_state()["seen"]), 1)

    def test_sql_window_keeps_clock_source_and_net_proof_exclusions(self):
        candidate = self._ready()
        decision = T0+timedelta(minutes=10)
        direction, _ = observation(candidate, "gated-direction", decision)
        trade, _ = observation(candidate, "gated-net", decision, trade=True)
        cases = [
            (dict(direction, known_at=(decision+timedelta(seconds=1)).isoformat()),
             "NONPROSPECTIVE_OR_INCOMPLETE_TIMES"),
            (dict(direction, source_verified=False), "UNVERIFIED_OUTCOME"),
            (dict(trade, costs_verified=False), "UNVERIFIED_NET_COST_OR_RISK"),
            (dict(trade, knowledge_cashflows=dict(trade["knowledge_cashflows"], source_evidence_hash="relabelled")),
             "UNVERIFIED_NET_COST_OR_RISK"),
        ]
        for row, reason in cases:
            with self.subTest(reason=reason):
                self._clear_consumed_state()
                whole = K.run_batch(self.connect, [row], now=row["observed_at"])
                expected = self._consumed_state()
                self._clear_consumed_state()
                windowed = K.run_batch(self.connect, [row], now=row["observed_at"], trial_window=(0, 1))
                self.assertEqual(windowed, whole)
                self.assertEqual(self._consumed_state(), expected)
                self.assertEqual(windowed["reasons"][reason], 1)
                self.assertEqual(windowed["counts"], {})

    def test_sql_catalog_immutable_audit_restore_and_revocation(self):
        candidate = self._ready()
        first = K.capture(self.rule, now=T0)
        K.refresh_catalog(self.connect, now=T0+timedelta(minutes=1))
        self.assertEqual(K.capture(self.rule, now=T0+timedelta(minutes=1)), first)
        K._VERIFIED_EPOCH = ["old", 0]
        K.restore(self.connect, now=T0+timedelta(minutes=2))
        self.assertIsNone(K._VERIFIED_EPOCH)
        self.assertEqual(K.capture(self.rule, now=T0+timedelta(minutes=2)), first)
        with self.connect() as c:
            c.execute("UPDATE knowledge_candidates SET metadata=%s::jsonb", (json.dumps(dict(llm_audit=dict(AUDIT, decision="REJECT"))),))
        K.refresh_catalog(self.connect, now=T0+timedelta(minutes=3))
        K.refresh_catalog(self.connect, now=T0+timedelta(minutes=3))
        self.assertIsNone(K.capture(self.rule, now=T0+timedelta(minutes=3)))
        self.assertEqual(candidate["definition_hash"], first["definition_hash"])

    def test_sql_retry_idempotency_rollback_and_nonoverlapping_receipts(self):
        candidate = self._ready()
        row, _ = observation(candidate, 1, T0+timedelta(minutes=10))
        with patch.object(STORE, "publish_snapshot_in_transaction", side_effect=RuntimeError("commit blocked")):
            with self.assertRaises(RuntimeError):
                K.run_batch(self.connect, [row], now=row["observed_at"])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM knowledge_validation_seen").fetchone()["n"], 0)
            self.assertEqual(c.execute("SELECT registered FROM knowledge_validation_family").fetchone()["registered"], 0)
        one = K.run_batch(self.connect, [row], now=row["observed_at"])
        two = K.run_batch(self.connect, [row], now=row["observed_at"])
        self.assertEqual(one["counts"], two["counts"])
        self.assertEqual(two["counts"]["direction"], 1)
        overlap, _ = observation(candidate, 2, T0+timedelta(minutes=11))
        result = K.run_batch(self.connect, [overlap], now=overlap["observed_at"])
        self.assertEqual(result["counts"]["direction"], 1)
        self.assertEqual(result["reasons"]["OVERLAPPING_MARKET_IDEA"], 1)

    def test_sql_corrected_source_receipt_revokes_its_exact_candidate(self):
        candidate = self._ready()
        row, _ = observation(candidate, 1, T0+timedelta(minutes=10))
        K.run_batch(self.connect, [row], now=row["observed_at"])
        corrected = dict(row, evidence_hash="corrected-immutable-source", evidence_valid=False)
        result = K.run_batch(self.connect, [corrected], now=row["observed_at"])
        self.assertEqual(result["candidates"][0]["state"], "REVOKED")
        self.assertEqual(result["counts"]["revoked"], 1)
        self.assertFalse(result["profiles"])

    def test_sql_catalog_statement_timeout_is_explicit_and_checkpoint_retryable(self):
        self._ready()
        class Budget:
            sql_timeout_ms = 2000
            def check(self):
                return None
        with K._transaction(self.connect, Budget()) as c:
            self.assertEqual(c.execute("SHOW statement_timeout").fetchone()["statement_timeout"], "2s")
            self.assertEqual(c.execute("SHOW lock_timeout").fetchone()["lock_timeout"], "250ms")

    def _catalog_query(self, after="ZZ_CATALOG_RULE_"):
        class Capture:
            def execute(self, sql, args=None):
                self.sql, self.args = sql, args
                return self
            def fetchall(self):
                return []
        capture = Capture()
        K._catalog_rules(capture, {"rule_after": after}, T0, None)
        return capture.sql, capture.args

    def _seed_catalog_probe_rules(self):
        noise = "SYNTHETIC_CATALOG_HISTORY:"+"".join(
            hashlib.sha256(("catalog-"+str(i)).encode()).hexdigest() for i in range(128))
        sources = ["AUTO_CAT_SHARED", "AUTO_CAT_SHARED", "AUTO_CAT_NO_SOURCE", "AUTO_CAT_NO_AUDIT",
                   "AUTO_CAT_NULL_AUDIT", "KNOWN_CAT_SOURCE", None, "AUTO_CAT_NULL_FIELDS", "AUTO_CAT_OVERSIZED"]
        ids = ["ZZ_CATALOG_RULE_"+str(i).zfill(2) for i in range(len(sources))]
        with self.connect() as c:
            for sid in sorted(set(sources)-{None, "AUTO_CAT_NO_SOURCE"}):
                source = dict(ACADEMIC, source_id=sid, title=noise*2, authors=noise,
                              url="https://example.org/"+noise, claim=noise*2)
                if sid == "AUTO_CAT_NULL_FIELDS":
                    source = dict.fromkeys(ACADEMIC)
                    source.update(source_id=sid, title="Nullable source fields")
                c.execute("""INSERT INTO knowledge_sources
                    (source_id,title,authors,year,source_type,url,evidence_grade,claim)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    tuple(source[k] for k in ("source_id", "title", "authors", "year",
                                              "source_type", "url", "evidence_grade", "claim")))
            for i, (rid, sid) in enumerate(zip(ids, sources)):
                rule = dict(RULE, rule_id=rid, source_id=sid)
                if i == 8:
                    rule.update(conditions=[dict(field="trend_score", padding=noise)],
                                hypothesis=noise, mechanism=noise, formalization_note=noise)
                c.execute("INSERT INTO knowledge_rules VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s)",
                    tuple(json.dumps(rule[k]) if k in ("asset_scope", "horizons", "conditions") else rule[k]
                          for k in ("rule_id", "source_id", "agent", "asset_scope", "horizons", "action",
                                    "status", "conditions", "prior_weight", "hypothesis", "mechanism", "formalization_note")))
            # These interspersed seed/inactive rules must not consume the page.
            for suffix, sid in (("00_SEED", "SEED_NOT_REGISTERED"), ("04_INACTIVE", "SEED_INACTIVE")):
                c.execute("""INSERT INTO knowledge_rules
                    SELECT %s,%s,agent,asset_scope,horizons,action,status,conditions,
                        prior_weight,hypothesis,mechanism,formalization_note
                    FROM knowledge_rules WHERE rule_id=%s""", ("ZZ_CATALOG_RULE_"+suffix, sid, self.rule["rule_id"]))
            for rid, sid, active in ((ids[5], sources[5], True), (ids[6], "KNOWN_NULL_SOURCE", True),
                                     ("ZZ_CATALOG_RULE_04_INACTIVE", "SEED_INACTIVE", False)):
                c.execute("""INSERT INTO knowledge_validation_rules
                    (rule_key,rule_id,source_id,version,payload,active,checked_at)
                    VALUES(%s,%s,%s,%s,'{}'::jsonb,%s,%s)""",
                    ("known-"+rid, rid, sid, K.VERSION, active, T0))
            for sid, audit, revision in (
                    ("AUTO_CAT_SHARED", json.dumps(AUDIT), 3),
                    ("AUTO_CAT_NO_SOURCE", json.dumps(AUDIT), 11),
                    ("AUTO_CAT_NULL_AUDIT", None, 7),
                    ("KNOWN_CAT_SOURCE", json.dumps(AUDIT), 13),
                    ("AUTO_CAT_NULL_FIELDS", json.dumps(AUDIT), 17),
                    ("AUTO_CAT_OVERSIZED", "null", 19)):
                c.execute("""INSERT INTO knowledge_validation_source_audits
                    (source_id,candidate_id,audit,audit_hash,revision,processed_at,checked_at)
                    VALUES(%s,%s,%s::jsonb,'synthetic-audit-hash',%s,%s,%s)""",
                    (sid, "candidate-"+sid, audit, revision, T0,
                     T0-timedelta(seconds=K.CATALOG_TTL+1) if sid == "KNOWN_CAT_SOURCE" else T0))
        return ids, noise

    def test_sql_catalog_page_keeps_legacy_projection_eligibility_and_missing_rows(self):
        ids, _ = self._seed_catalog_probe_rules()
        all_rows = []
        after = "ZZ_CATALOG_RULE_"
        with K._transaction(self.connect) as c:
            for size in (8, 1, 0):
                sql, args = self._catalog_query(after)
                expected = c.execute(LEGACY_CATALOG_RULES_SQL, args).fetchall()
                actual = c.execute(sql, args).fetchall()
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), size)
                all_rows.extend(actual)
                if actual:
                    after = actual[-1]["rule_id"]
        self.assertEqual([r["rule_id"] for r in all_rows], ids)
        rows = {r["rule_id"]: r for r in all_rows}
        self.assertEqual(rows[ids[0]]["source"], rows[ids[1]]["source"])
        self.assertEqual(rows[ids[0]]["audit"], rows[ids[1]]["audit"])
        self.assertEqual(rows[ids[2]]["source"], dict.fromkeys(ACADEMIC))
        self.assertEqual(rows[ids[2]]["audit"], AUDIT)
        self.assertEqual(rows[ids[2]]["audit_revision"], 11)
        self.assertIsNone(rows[ids[3]]["audit"])
        self.assertIsNone(rows[ids[3]]["audit_checked_at"])
        self.assertIsNone(rows[ids[3]]["audit_revision"])
        self.assertIsNone(rows[ids[4]]["audit"])
        self.assertEqual(rows[ids[4]]["audit_checked_at"], T0)
        self.assertEqual(rows[ids[4]]["audit_revision"], 7)
        self.assertEqual(rows[ids[5]]["audit_checked_at"], T0-timedelta(seconds=K.CATALOG_TTL+1))
        self.assertEqual(rows[ids[6]]["source"], dict.fromkeys(ACADEMIC))
        self.assertIsNone(rows[ids[6]]["audit"])
        self.assertIsNone(rows[ids[7]]["source"]["authors"])
        self.assertIsNone(rows[ids[7]]["source"]["url"])
        for key in ("conditions", "hypothesis", "mechanism", "formalization_note"):
            self.assertIsNone(rows[ids[8]][key])
        self.assertIsNone(rows[ids[8]]["audit"])
        self.assertEqual(rows[ids[8]]["audit_revision"], 19)
        self.assertEqual({k: len(rows[ids[0]]["source"][k]) for k in ("title", "authors", "url", "claim")},
                         {"title": 512, "authors": 512, "url": 1024, "claim": 1024})

    def _catalog_read_plan(self, c, query, args, *, generic=False):
        if not generic:
            rows = c.execute(query, args).fetchall()
            plan = c.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "+query, args).fetchone()["QUERY PLAN"][0]
            return rows, plan
        from psycopg.sql import SQL, Literal
        prepared = query.replace("%s", "$1", 1).replace("%s", "$2", 1)
        c.execute("PREPARE catalog_rule_probe(text,integer) AS "+prepared)
        try:
            with c.transaction():
                c.execute("SET LOCAL plan_cache_mode='force_generic_plan'")
                run = SQL("EXECUTE catalog_rule_probe({}, {})").format(Literal(args[0]), Literal(args[1]))
                rows = c.execute(run).fetchall()
                plan = c.execute(SQL("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ")+run).fetchone()["QUERY PLAN"][0]
                return rows, plan
        finally:
            c.execute("DEALLOCATE catalog_rule_probe")

    def test_sql_catalog_page_uses_bounded_pk_probes_amid_toasted_sources_and_audits(self):
        ids, noise = self._seed_catalog_probe_rules()
        query, args = self._catalog_query()
        archive_audit = json.dumps(dict(AUDIT, synthetic_unused_history=noise[:4096]))
        with self.connect() as c:
            for start in range(0, 4000, 200):
                c.execute("""INSERT INTO knowledge_sources
                    (source_id,title,authors,year,source_type,url,evidence_grade,claim)
                    SELECT 'ARCHIVE_SOURCE_'||n,%s::text,%s::text,2025,'synthetic_archive',%s::text,'UNVERIFIED',%s::text
                    FROM generate_series(%s::integer,%s::integer) n""",
                    (noise*2, noise, "https://example.org/"+noise, noise*2, start, start+199))
                c.execute("""INSERT INTO knowledge_validation_source_audits
                    (source_id,candidate_id,audit,audit_hash,revision,processed_at,checked_at)
                    SELECT 'ARCHIVE_SOURCE_'||n,'ARCHIVE_CANDIDATE_'||n,%s::jsonb,
                        'synthetic-archive-hash',1,%s::timestamptz,%s::timestamptz
                    FROM generate_series(%s::integer,%s::integer) n""",
                    (archive_audit, T0, T0, start, start+199))
            for table in ("knowledge_rules", "knowledge_sources", "knowledge_validation_source_audits",
                          "knowledge_validation_rules"):
                c.execute("ANALYZE "+table)
            expected = c.execute(LEGACY_CATALOG_RULES_SQL, args).fetchall()
            self.assertEqual([r["rule_id"] for r in expected], ids[:K.CATALOG_RULE_BATCH])
            physical = {r["relname"]: r["toast_bytes"] for r in c.execute("""SELECT relname,
                pg_relation_size(reltoastrelid) AS toast_bytes FROM pg_class WHERE oid IN
                ('knowledge_sources'::regclass,'knowledge_validation_source_audits'::regclass)""").fetchall()}
            self.assertTrue(all(n > 1_000_000 for n in physical.values()))
            self.assertEqual(len(physical), 2)
            def nodes(plan):
                yield plan
                for child in plan.get("Plans", []):
                    yield from nodes(child)
            # The fixture is prepared outside the query transaction. Both
            # measured plans use the unchanged 2s/250ms production settings.
            with K._transaction(lambda: nullcontext(c)) as bounded:
                self.assertEqual(bounded.execute("SHOW statement_timeout").fetchone()["statement_timeout"], "2s")
                self.assertEqual(bounded.execute("SHOW lock_timeout").fetchone()["lock_timeout"], "250ms")
                for generic in (False, True):
                    with self.subTest(plan="generic" if generic else "default"):
                        rows, plan = self._catalog_read_plan(bounded, query, args, generic=generic)
                        self.assertEqual(rows, expected)
                        probes = [n for n in nodes(plan["Plan"]) if n.get("Relation Name") in physical]
                        self.assertCountEqual([n["Relation Name"] for n in probes], list(physical))
                        for node in probes:
                            self.assertEqual(node["Node Type"], "Index Scan")
                            self.assertEqual(node["Index Name"], node["Relation Name"]+"_pkey")
                            self.assertRegex(node["Index Cond"], r"\bsource_id\s*=\s*p\.source_id\b")
                            self.assertGreater(node["Actual Loops"], 0)
                            self.assertLessEqual(node["Actual Loops"], K.CATALOG_RULE_BATCH)
                            self.assertLessEqual(node["Actual Rows"], 1)
                        print("CATALOG_RULE_PAGE_SYNTHETIC_PLAN "+json.dumps(dict(
                            plan_mode="generic" if generic else "default", archive_sources=4000,
                            archive_audits=4000, page_rows=len(rows), toast_bytes=physical,
                            execution_ms=plan["Execution Time"], statement_timeout="2s",
                            probes=[dict(table=n["Relation Name"], index=n["Index Name"],
                                         loops=n["Actual Loops"], rows=n["Actual Rows"]) for n in probes])), flush=True)

    def test_sql_catalog_advances_bounded_pages_and_preserves_registration(self):
        with self.connect() as c:
            # Seed rules have no original compiler audit and must not consume
            # the scarce eight-rule publication page on the 60-second lane.
            c.execute("""INSERT INTO knowledge_rules SELECT 'A_SEED_'||i,'SEED',agent,asset_scope,horizons,action,status,
                conditions,prior_weight,hypothesis,mechanism,formalization_note
                FROM knowledge_rules CROSS JOIN generate_series(1,100) AS i WHERE rule_id=%s""", (self.rule["rule_id"],))
            for i in range(34):
                c.execute("""INSERT INTO knowledge_rules SELECT %s,source_id,agent,asset_scope,horizons,action,status,
                    conditions,prior_weight,hypothesis,mechanism,formalization_note FROM knowledge_rules WHERE rule_id=%s""",
                    ("RULE_"+str(i).zfill(3), self.rule["rule_id"]))
        cursor = {}; pages = []
        for i in range(10):
            result = K.refresh_catalog(self.connect, now=T0+timedelta(seconds=i), cursor=cursor)
            cursor = result["cursor"]
            if result["phase"] == "rules":
                pages.append(result["processed"])
        self.assertEqual(pages, [8, 8, 8, 8, 3])
        self.assertEqual(cursor["rule_after"], "")
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM knowledge_validation_rules").fetchone()["n"], 35)

    def test_sql_previously_registered_rule_with_removed_auto_source_is_invalidated(self):
        self._ready()
        with self.connect() as c:
            c.execute("UPDATE knowledge_rules SET source_id='SEED'")
        K.refresh_catalog(self.connect, now=T0+timedelta(minutes=1))
        result = K.refresh_catalog(self.connect, now=T0+timedelta(minutes=1))
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["audited"], 0)
        self.assertIsNone(K.capture(self.rule, now=T0+timedelta(minutes=1)))

    def test_sql_external_cursor_has_no_nested_lease_and_failed_page_replays(self):
        cursor = {}
        with patch.object(STORE, "claim_job", side_effect=AssertionError("nested lease")), \
             patch.object(STORE, "checkpoint_job", side_effect=AssertionError("nested checkpoint")):
            result = K.refresh_catalog(self.connect, now=T0, cursor=cursor)
            self.assertEqual(cursor, {})
            cursor = result["cursor"]
            saved = deepcopy(cursor)
            with patch.object(K, "_catalog_rules", side_effect=RuntimeError("cancelled")):
                with self.assertRaises(RuntimeError):
                    K.refresh_catalog(self.connect, now=T0, cursor=cursor)
            self.assertEqual(cursor, saved)
            result = K.refresh_catalog(self.connect, now=T0, cursor=cursor)
            self.assertEqual(result["audited"], 1)

    def test_sql_newest_original_audit_revokes_before_rule_page_and_cannot_resurrect_identity(self):
        self._ready()
        original = K.capture(self.rule, now=T0)
        with self.connect() as c:
            c.execute("""INSERT INTO knowledge_candidates VALUES(%s,%s,%s::jsonb,now()+interval '1 day')""",
                ("z-latest", self.candidate_id, json.dumps(dict(llm_audit=dict(AUDIT, decision="REJECT")))))
        result = K.refresh_catalog(self.connect, now=T0+timedelta(minutes=1))
        self.assertEqual(result["phase"], "audit_index")
        self.assertEqual(result["invalidated_sources"], 1)
        self.assertIsNone(K.capture(self.rule, now=T0+timedelta(minutes=1)))
        with self.connect() as c:
            c.execute("UPDATE knowledge_candidates SET metadata=%s::jsonb WHERE candidate_id='z-latest'", (json.dumps(dict(llm_audit=AUDIT)),))
        for _ in range(3):
            K.refresh_catalog(self.connect, now=T0+timedelta(minutes=2))
        restored = K.capture(self.rule, now=T0+timedelta(minutes=2))
        self.assertIsNotNone(restored)
        self.assertEqual(original["audit_hash"], restored["audit_hash"])
        self.assertNotEqual(original["rule_key"], restored["rule_key"])

    def test_sql_initial_metadata_scan_finishes_before_rule_publication(self):
        with self.connect() as c:
            for i in range(K.CATALOG_AUDIT_BATCH):
                c.execute("INSERT INTO knowledge_candidates VALUES(%s,NULL,%s::jsonb,now())",
                          ("a-"+str(i).zfill(3), json.dumps(dict(unrelated="x"*100000))))
        first = K.refresh_catalog(self.connect, now=T0, cursor={})
        self.assertEqual(first["processed"], K.CATALOG_AUDIT_BATCH)
        self.assertEqual(first["status"], "PROGRESS")
        self.assertFalse(first["cursor"]["audits_complete"])
        self.assertIsNone(K.capture(self.rule, now=T0))
        second = K.refresh_catalog(self.connect, now=T0, cursor=first["cursor"])
        self.assertTrue(second["cursor"]["audits_complete"])
        self.assertEqual(second["processed"], 1)
        third = K.refresh_catalog(self.connect, now=T0, cursor=second["cursor"])
        self.assertEqual(third["audited"], 1)

    def test_sql_future_pipeline_promotes_and_cold_reload_restores_oos_stamp(self):
        candidate = self._ready()
        training = [observation(candidate, i, T0+timedelta(minutes=10+10*i))[0] for i in range(80)]
        for offset in range(0, 80, 32):
            batch = training[offset:offset+32]; at = K._time(batch[-1]["observed_at"])
            K.refresh_catalog(self.connect, now=at)
            K.refresh_catalog(self.connect, now=at)
            K.run_batch(self.connect, batch, now=at)
        with self.connect() as c:
            candidate = c.execute("SELECT payload FROM knowledge_validation_trials WHERE trial_id=%s", (candidate["trial_id"],)).fetchone()["payload"]
        self.assertEqual(candidate["state"], "OOS")
        K._TRIALS.clear()
        start = K._time(candidate["oos_registered_at"])+timedelta(days=1)
        old_stamp_candidate = fresh_candidate()
        # Use this candidate's proper registry identity but a deliberately cold
        # producer's TRAIN stamp. Loading it may not reuse any training outcome.
        old_stamp_candidate = dict(candidate, evaluation_id=None, state="TRAINING")
        cold, _ = observation(old_stamp_candidate, "cold", start, phase="TRAIN")
        K.refresh_catalog(self.connect, now=cold["observed_at"])
        K.refresh_catalog(self.connect, now=cold["observed_at"])
        answer = K.run_batch(self.connect, [cold], now=cold["observed_at"])
        self.assertEqual(answer["reasons"]["OOS_NOT_PREREGISTERED_BEFORE_DECISION"], 1)
        self.assertEqual(K._TRIALS[K._base_key(candidate["rule_key"], candidate["scope"])]["evaluation_id"], candidate["evaluation_id"])
        outcomes = []
        for i in range(100):
            for trade in (False, True):
                row, _ = observation(candidate, "future-"+str(i), start+timedelta(days=1, hours=12*i),
                                     direction="LONG" if i % 2 == 0 else "SHORT", trade=trade)
                outcomes.append(row)
        for offset in range(0, len(outcomes), 32):
            batch = outcomes[offset:offset+32]; at = K._time(batch[-1]["observed_at"])
            K.refresh_catalog(self.connect, now=at)
            K.refresh_catalog(self.connect, now=at)
            answer = K.run_batch(self.connect, batch, now=at)
        with self.connect() as c:
            final = c.execute("SELECT payload FROM knowledge_validation_trials WHERE trial_id=%s", (candidate["trial_id"],)).fetchone()["payload"]
        self.assertEqual(final["state"], "PROMOTED")
        self.assertEqual(final["direction"]["n"], 100)
        self.assertEqual(final["net"]["n"], 100)
        self.assertEqual(answer["profiles"], [])  # no same-process full receipt sweep
        self.assertEqual(final["training"]["n"], 80)


if __name__ == "__main__":
    unittest.main()
