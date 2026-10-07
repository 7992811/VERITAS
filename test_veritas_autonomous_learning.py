from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

import veritas_autonomous_learning as AL


CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = CREATED+timedelta(days=20)
SOURCE = {"primary_source": "OBSERVED", "contract_id": "BTC-SPOT"}


def observation(index, *, training=False, probability=.55, correct=True):
    decision = CREATED+timedelta(days=-40+index if training else 1+index//4,
                                 hours=0 if training else 2*(index % 4))
    outcome = decision+timedelta(hours=1)
    return {"episode_key": "episode-"+str(index), "idea_id": ("train-" if training else "idea-")+str(index),
            "asset": "BTC", "horizon": "1h", "regime": "TREND", "direction": "LONG",
            "decision_at": decision.isoformat(), "known_at": decision.isoformat(),
            "outcome_at": outcome.isoformat(), "observed_at": outcome.isoformat(),
            "forward_return": .01 if correct else -.01, "predicted_probability": probability,
            "source_identity": SOURCE, "source_verified": True, "independence_verified": True,
            "evidence_valid": True, "evidence_hash": "proof-"+str(index), "evidence_version": "PROOF_V1",
            "policy_hash": "immutable-policy"}


def candidate(kind="CALIBRATION"):
    return AL.register_candidate(AL.scope_for(observation(0)),
                                 {"n": 32, "residual_sum": 5., "evidence_hash": "training-proof"}, kind, CREATED)


def paired(index, c, *, weak=None):
    weak = index % 4 == 0 if weak is None else weak
    row = observation(index, probability=.55 if weak else .8)
    net = -1. if weak else 2.
    row.update(proof_kind="SIMULATED_SIZE_ON_OBSERVED_PATH", candidate_id=c["candidate_id"],
               candidate_decision_at=row["decision_at"], baseline_net_r=net,
               candidate_net_r=.9*net if weak else net, baseline_risk_r=1.,
               candidate_risk_r=.9 if weak else 1., risk_cap_r=1.,
               costs_verified=True, risk_verified=True)
    return row


def uncalibrated_pair(index, c=None, *, training=False, rr=None, fraction=.50):
    rr = (1.20 if index % 4 == 0 else 2.) if rr is None else rr
    row = observation(index, training=training, probability=None, correct=rr >= 1.5)
    net = -1. if rr < 1.5 else 2.
    execution = AL.size_execution(None, fraction=fraction, step=.05, cap=.50,
                                  kind='SIZE_DOWN_UNCALIBRATED', net_reward_risk=rr)
    factor = execution['effective_multiplier']
    row.update(proof_kind='SIMULATED_SIZE_ON_OBSERVED_PATH', net_reward_risk=rr,
               candidate_id=c['candidate_id'] if c else None,
               candidate_decision_at=row['decision_at'] if c else None,
               baseline_net_r=net, candidate_net_r=net*factor,
               baseline_risk_r=1., candidate_risk_r=factor, risk_cap_r=1.,
               costs_verified=True, risk_verified=True, size_execution=execution)
    return row


class PureLearningTests(unittest.TestCase):
    def test_observed_direction_learning_does_not_claim_profitability(self):
        result = AL.evaluate(candidate(), [observation(i) for i in range(64)], now=NOW)
        self.assertEqual(result["state"], "promoted")
        self.assertEqual(result["evidence"]["n"], 64)
        self.assertEqual(result["evidence"]["days"], 16)
        self.assertFalse(result["profitability_proven"])
        advice = AL.apply_advice([result], observation(0), now=NOW)
        self.assertAlmostEqual(advice["calibrated_probability"], .60)
        self.assertEqual(advice["size_multiplier"], 1.)
        self.assertFalse(advice["profitability_proven"])

    def test_future_outcomes_cannot_relabel_pre_registration_decisions(self):
        row = observation(0)
        row["decision_at"] = (CREATED-timedelta(hours=1)).isoformat()
        row["known_at"] = row["decision_at"]
        result = AL.evaluate(candidate(), [row], now=NOW)
        self.assertEqual(result["stats"]["n"], 0)
        self.assertIn("PRE_REGISTRATION_DECISION", result["excluded"])

    def test_future_information_missing_proofs_and_nan_are_excluded(self):
        cases = (("known_at", (CREATED+timedelta(days=19)).isoformat()),
                 ("observed_at", (NOW+timedelta(days=1)).isoformat()),
                 ("source_verified", False), ("independence_verified", False),
                 ("evidence_valid", False), ("forward_return", float("nan")),
                 ("outcome_at", None))
        for field, value in cases:
            with self.subTest(field=field):
                row = observation(0); row[field] = value
                normalized, reason = AL.normalize_observation(row, NOW)
                self.assertIsNone(normalized)
                self.assertIsNotNone(reason)

    def test_missing_probability_and_costs_are_not_zero(self):
        row = observation(0); row.pop("predicted_probability")
        result = AL.evaluate(candidate(), [row], now=NOW)
        self.assertEqual(result["stats"]["n"], 0)
        size = candidate("SIZE_DOWN_WEAK_SIGNAL")
        row = paired(0, size); row.pop("candidate_net_r")
        result = AL.evaluate(size, [row], now=NOW)
        self.assertEqual(result["stats"]["n"], 0)
        self.assertIn("WAIT_VERIFIED_COSTS_AND_RISK", result["reasons"])

    def test_duplicate_and_overlapping_ideas_do_not_increase_sample(self):
        first = observation(0)
        overlap = dict(first, idea_id="another-report")
        result = AL.evaluate(candidate(), [first, first, overlap], now=NOW)
        self.assertEqual(result["stats"]["n"], 1)
        replay = AL.evaluate(result, [first], now=NOW)
        self.assertEqual(replay["stats"]["n"], 1)

    def test_large_fast_sample_cannot_replace_temporal_coverage(self):
        rows = []
        for i in range(256):
            row = observation(i)
            start = CREATED+timedelta(days=1, minutes=2*i)
            row.update(decision_at=start.isoformat(), known_at=start.isoformat(),
                       outcome_at=(start+timedelta(minutes=1)).isoformat(),
                       observed_at=(start+timedelta(minutes=1)).isoformat())
            rows.append(row)
        result = AL.evaluate(candidate(), rows, now=NOW)
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result["reasons"], ["INSUFFICIENT_TEMPORAL_COVERAGE"])
        self.assertEqual(result["completed_looks"], [64, 128, 256])

    def test_contract_mutation_revokes_instead_of_silently_tuning(self):
        c = candidate(); c["proposal"]["probability_delta"] = -.05
        result = AL.evaluate(c, [observation(0)], now=NOW)
        self.assertEqual(result["state"], "degraded")
        self.assertEqual(result["reasons"], ["CANDIDATE_CONTRACT_CHANGED"])
        self.assertEqual(AL.apply_advice([result], observation(0), now=NOW)["status"], "neutral")

    def test_size_requires_prospective_stamp_all_costs_and_same_budget(self):
        c = candidate("SIZE_DOWN_WEAK_SIGNAL")
        for field, value in (("candidate_decision_at", CREATED.isoformat()),
                             ("costs_verified", False), ("risk_verified", False),
                             ("risk_cap_r", .5), ("candidate_risk_r", 1.2),
                             ("candidate_net_r", 100.)):
            with self.subTest(field=field):
                row = paired(0, c); row[field] = value
                result = AL.evaluate(c, [row], now=NOW)
                self.assertEqual(result["stats"]["n"], 0)

    def test_conditional_size_preserves_unaffected_opportunities_and_can_improve(self):
        c = candidate("SIZE_DOWN_WEAK_SIGNAL")
        result = AL.evaluate(c, [paired(i, c) for i in range(64)], now=NOW)
        self.assertEqual(result["state"], "promoted")
        self.assertTrue(result["profitability_proven"])
        self.assertEqual(result["evidence"]["n"], 64)
        self.assertAlmostEqual(result["evidence"]["mean_delta"], .025)
        self.assertAlmostEqual(result["evidence"]["mean_candidate"], 1.275)
        self.assertEqual(AL.apply_advice([result], observation(0), now=NOW)["size_multiplier"], .9)
        self.assertEqual(AL.apply_advice([result], observation(0, probability=.8), now=NOW)["size_multiplier"], 1.)

    def test_lower_losses_without_profitable_outcomes_does_not_promote_size(self):
        c = candidate("SIZE_DOWN_WEAK_SIGNAL")
        result = AL.evaluate(c, [paired(i, c, weak=True) for i in range(64)], now=NOW)
        self.assertEqual(result["state"], "evaluating")
        self.assertEqual(result["reasons"], ["PROFITABILITY_OR_DRAWDOWN_GATE_FAILED"])

    def test_expired_or_different_source_policy_regime_advice_is_neutral(self):
        c = AL.evaluate(candidate(), [observation(i) for i in range(64)], now=NOW)
        for field, value in (("policy_hash", "new-policy"), ("regime", "CHOP"),
                             ("source_identity", {"contract_id": "DIFFERENT"})):
            row = observation(0); row[field] = value
            self.assertEqual(AL.apply_advice([c], row, now=NOW)["status"], "neutral")
        self.assertEqual(AL.apply_advice([c], observation(0), now=NOW+timedelta(days=8))["status"], "neutral")
        self.assertEqual(AL.evaluate(c, [], now=NOW+timedelta(days=8))["state"], "degraded")

    def test_prospective_deterioration_revokes_promoted_calibration(self):
        c = AL.evaluate(candidate(), [observation(i) for i in range(64)], now=NOW)
        rows = []
        for i in range(32):
            row = observation(80+i, correct=False)
            decision = NOW+timedelta(days=i//5, hours=2*(i % 5))
            row.update(decision_at=decision.isoformat(), known_at=decision.isoformat(),
                       outcome_at=(decision+timedelta(hours=1)).isoformat(),
                       observed_at=(decision+timedelta(hours=1)).isoformat())
            rows.append(row)
        # Each observation is assessed at arrival, not with a future clock that
        # would already expire the original proof before monitoring can run.
        for row in rows:
            c = AL.evaluate(c, [row], now=row["observed_at"])
            if c["state"] == "degraded":
                break
        self.assertEqual(c["state"], "degraded")
        self.assertEqual(c["reasons"], ["PROSPECTIVE_DETERIORATION"])

    def test_unexecutable_size_step_is_unchanged_but_still_an_observation(self):
        c = candidate("SIZE_DOWN_WEAK_SIGNAL")
        row = paired(0, c)
        execution = AL.size_execution(.55, fraction=.10, step=.05, cap=.5)
        self.assertEqual(execution["effective_multiplier"], 1.)
        row.update(size_execution=execution, candidate_net_r=-1., candidate_risk_r=1.)
        result = AL.evaluate(c, [row], now=NOW)
        self.assertEqual(result["stats"]["n"], 1)
        self.assertEqual(result["stats"]["sum_delta"], 0.)
        row["size_execution"] = dict(execution, fraction_after=.05, effective_multiplier=.5)
        self.assertEqual(AL.evaluate(c, [row], now=NOW)["stats"]["n"], 0)

    def test_candidate_stamp_is_predecision_and_freezes_actual_position_step(self):
        c = candidate("SIZE_DOWN_WEAK_SIGNAL")
        row = dict(observation(0), fraction=.10, position_step=.05, max_fraction=.5)
        self.assertEqual(AL.freeze_candidates({"candidates": [c]}, row, now=CREATED), [])
        stamps = AL.freeze_candidates({"candidates": [c]}, row, now=CREATED+timedelta(hours=1))
        self.assertEqual(len(stamps), 1)
        self.assertEqual(stamps[0]["size_multiplier"], 1.)
        self.assertEqual(stamps[0]["base_probability"], .55)

    def test_maximum_hot_state_fits_real_store_limits(self):
        import veritas_learning_state as store
        state = AL.initial_state()
        for i in range(AL.MAX_SCOPES):
            state["scopes"][str(i)] = {"scope": {"asset": "BTC", "horizon": "1h", "regime": "TREND",
                "policy_hash": "a"*64, "source_key": "b"*64}, "n": 12345, "correct": 123,
                "probability_n": 123, "residual_sum": 12.345678901, "evidence_hash": "c"*64,
                "uncalibrated_n": 12345, "uncalibrated_evidence_hash": "d"*64,
                "registered_at_n": {"CALIBRATION": 12000, "SIZE_DOWN_WEAK_SIGNAL": 12000,
                                    "SIZE_DOWN_UNCALIBRATED": 12000}}
        for i in range(AL.MAX_CANDIDATES):
            c = candidate(AL.KINDS[i % len(AL.KINDS)])
            for stats in (c["stats"], c["monitor"]):
                for day in range(AL.MAX_DAYS):
                    AL._add(stats, f"2026-01-{day+1:02}", .612345678901, .512345678901, .100000000001, True)
            c["evidence"] = AL._summary(c["stats"])
            c["monitor_evidence"] = AL._summary(c["monitor"])
            state["candidates"][str(i)] = c
        encoded = store._json(state, store.MAX_SNAPSHOT_BYTES)
        self.assertLess(len(encoded.encode()), store.MAX_SNAPSHOT_BYTES)

    def test_repeated_trials_spend_a_frozen_global_error_budget(self):
        first = candidate()
        later = AL.register_candidate(first["scope"], {"n": 32, "residual_sum": 5.},
                                      "CALIBRATION", CREATED, trial_index=100)
        self.assertLess(later["criteria"]["per_look_alpha"], first["criteria"]["per_look_alpha"])
        self.assertGreater(later["criteria"]["block_interval_multiplier"], first["criteria"]["block_interval_multiplier"])
        self.assertNotEqual(later["contract_hash"], first["contract_hash"])

    def test_uncalibrated_size_uses_frozen_rr_without_inventing_probability(self):
        c = candidate('SIZE_DOWN_UNCALIBRATED')
        result = AL.evaluate(c, [uncalibrated_pair(i, c) for i in range(64)], now=NOW)
        self.assertEqual(result['state'], 'promoted')
        self.assertTrue(result['profitability_proven'])
        self.assertEqual(result['evidence']['n'], 64)
        self.assertEqual(result['evidence']['days'], 16)
        self.assertAlmostEqual(result['evidence']['mean_delta'], .025)
        self.assertAlmostEqual(result['evidence']['mean_candidate'], 1.275)
        low = dict(observation(0, probability=None), net_reward_risk=1.20, confidence=.99)
        advice = AL.apply_advice([result], low, now=NOW)
        self.assertEqual(advice['size_multiplier'], .9)
        self.assertIsNone(advice['calibrated_probability'])
        self.assertEqual(advice['probability_delta'], 0.)
        for rr in (1.50, 2.):
            self.assertEqual(AL.apply_advice([result], dict(low, net_reward_risk=rr), now=NOW)['size_multiplier'], 1.)
        for changes in ({'predicted_probability': .55}, {'net_reward_risk': None},
                        {'net_reward_risk': -1.}, {'predicted_probability': float('nan')}):
            self.assertEqual(AL.apply_advice([result], dict(low, **changes), now=NOW)['status'], 'neutral')

    def test_uncalibrated_trial_keeps_prospective_cashflow_and_quantization_guards(self):
        c = candidate('SIZE_DOWN_UNCALIBRATED')
        row = uncalibrated_pair(0, c, fraction=.10)
        result = AL.evaluate(c, [row], now=NOW)
        self.assertEqual(result['stats']['n'], 1)
        self.assertEqual(result['stats']['sum_delta'], 0.)
        for field, value in (('predicted_probability', .55), ('net_reward_risk', None),
                             ('costs_verified', False), ('risk_verified', False),
                             ('candidate_decision_at', CREATED.isoformat()),
                             ('candidate_net_r', 0.), ('risk_cap_r', .5), ('size_execution', None)):
            with self.subTest(field=field):
                self.assertEqual(AL.evaluate(c, [dict(row, **{field:value})], now=NOW)['stats']['n'], 0)
        self.assertEqual(AL.evaluate(candidate(), [row], now=NOW)['stats']['n'], 0)
        self.assertEqual(AL.evaluate(candidate('SIZE_DOWN_WEAK_SIGNAL'), [row], now=NOW)['stats']['n'], 0)
        losing = AL.evaluate(c, [uncalibrated_pair(i, c, rr=1.2) for i in range(64)], now=NOW)
        self.assertEqual(losing['state'], 'evaluating')
        self.assertEqual(losing['reasons'], ['PROFITABILITY_OR_DRAWDOWN_GATE_FAILED'])

    def test_uncalibrated_stamp_has_original_rr_and_executable_size_with_no_probability(self):
        candidates = [candidate(kind) for kind in AL.KINDS]
        row = dict(observation(0, probability=None), net_reward_risk=1.2,
                   fraction=.10, position_step=.05, max_fraction=.50, confidence=.95)
        stamps = AL.freeze_candidates({'candidates':candidates}, row, now=CREATED+timedelta(hours=1))
        self.assertEqual(len(stamps), 1)
        self.assertEqual(stamps[0]['kind'], 'SIZE_DOWN_UNCALIBRATED')
        self.assertIsNone(stamps[0]['base_probability'])
        self.assertEqual(stamps[0]['net_reward_risk'], 1.2)
        self.assertEqual(stamps[0]['size_multiplier'], 1.)
        self.assertEqual(stamps[0]['size_execution']['fraction_after'], .10)
        row['fraction'] = .50
        stamp = AL.freeze_candidates({'candidates':candidates}, row, now=CREATED+timedelta(hours=1))[0]
        self.assertAlmostEqual(stamp['size_multiplier'], .9)
        self.assertAlmostEqual(stamp['size_execution']['fraction_after'], .45)
        self.assertEqual(AL.freeze_candidates({'candidates':candidates}, dict(row, net_reward_risk=None), now=NOW), [])
        old = AL.freeze_candidates({'candidates':candidates}, dict(row, predicted_probability=.55), now=CREATED+timedelta(hours=1))
        self.assertEqual({s['kind'] for s in old}, {'CALIBRATION','SIZE_DOWN_WEAK_SIGNAL'})

    def test_unchanged_opportunity_never_rounds_an_off_grid_original_allocation(self):
        for probability, kwargs in ((.80, {}), (None, {'kind':'SIZE_DOWN_UNCALIBRATED','net_reward_risk':2.})):
            with self.subTest(probability=probability):
                execution=AL.size_execution(probability,fraction=.56,step=.05,cap=.80,**kwargs)
                self.assertEqual(execution['fraction_after'],.56)
                self.assertEqual(execution['effective_multiplier'],1.)
                self.assertIsNone(AL.size_execution(probability,fraction=.56,step=.05,cap=.50,**kwargs))
        reduced=AL.size_execution(None,fraction=.50,step=.05,cap=.80,
                                   kind='SIZE_DOWN_UNCALIBRATED',net_reward_risk=1.20)
        self.assertAlmostEqual(reduced['fraction_after'],.45)
        self.assertAlmostEqual(reduced['effective_multiplier'],.90)


class LedgerDB:
    """Use SQLite transactions to exercise ledger+aggregate atomicity locally."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = lambda cursor, values: dict(zip([d[0] for d in cursor.description], values))
        self.db.executescript("""CREATE TABLE autonomous_learning_seen
            (idea_key TEXT PRIMARY KEY,scope_key TEXT,original_key TEXT,scope_epoch INTEGER,evidence_hash TEXT,valid BOOLEAN,observed_at TEXT);
            CREATE TABLE autonomous_learning_archive(candidate_id TEXT PRIMARY KEY,state TEXT,payload TEXT);
            CREATE TABLE autonomous_learning_scope_archive(epoch_key TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE autonomous_learning_cold_scopes(scope_key TEXT PRIMARY KEY,payload TEXT,updated_at TEXT);
            CREATE TABLE snapshots(name TEXT PRIMARY KEY,version TEXT,payload TEXT);""")

    def execute(self, sql, params=()):
        if sql.startswith("SET LOCAL"):
            return self.db.execute("SELECT 1")
        sql = sql.replace("%s", "?").replace("::jsonb", "")
        sql = sql.replace("now()", "datetime('now')")
        return self.db.execute(sql, tuple(p.isoformat() if isinstance(p, datetime) else p for p in params))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN")
        try:
            yield
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def load(self, c, name, version, **kwargs):
        row = c.execute("SELECT payload FROM snapshots WHERE name=%s AND version=%s", (name, version)).fetchone()
        return {"payload": json.loads(row["payload"])} if row else None

    def publish(self, c, name, version, payload, **kwargs):
        c.execute("INSERT INTO snapshots(name,version,payload) VALUES(%s,%s,%s) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                  (name, version, json.dumps(payload, allow_nan=False)))
        return True


class DurableLearningTests(unittest.TestCase):
    def setUp(self):
        self.db = LedgerDB()
        self.patches = [patch("veritas_learning_state.load_snapshot_in_transaction", self.db.load),
                        patch("veritas_learning_state.publish_snapshot_in_transaction", self.db.publish)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(self.db.db.close)

    def run_rows(self, rows, now=CREATED, **kwargs):
        return AL.run_batch(lambda: self.db, rows, now=now, **kwargs)

    def test_restart_replay_and_cross_timeframe_duplicates_do_not_inflate(self):
        rows = [observation(i, training=True) for i in range(32)]
        first = self.run_rows(rows)
        self.assertEqual(first["counts"]["direction"], 32)
        self.assertEqual(len(first["candidates"]), 2)
        replay = self.run_rows(rows)
        other_tf = dict(rows[0], horizon="4h", episode_key="another-fill", evidence_hash="different-horizon-outcome")
        replay = self.run_rows([other_tf])
        self.assertEqual(replay["counts"]["direction"], 32)
        self.assertEqual(len(replay["candidates"]), 2)
        self.assertFalse(any(c["state"] == "degraded" for c in replay["candidates"]))
        self.assertEqual({c["candidate_id"] for c in first["candidates"]},
                         {c["candidate_id"] for c in replay["candidates"]})

    def test_uncalibrated_trade_only_training_is_durable_independent_and_revocable(self):
        rows = [uncalibrated_pair(i, training=True) for i in range(32)]
        early = self.run_rows(rows[:31])
        self.assertEqual(early['candidates'], [])
        first = self.run_rows(rows[31:])
        self.assertEqual({c['kind'] for c in first['candidates']}, {'SIZE_DOWN_UNCALIBRATED'})
        self.assertEqual(first['candidates'][0]['training_n'], 32)
        self.assertEqual(first['lessons'][0]['probability_observations'], 0)
        self.assertEqual(first['lessons'][0]['uncalibrated_risk_observations'], 32)
        replay = self.run_rows(rows)
        self.assertEqual(replay['counts']['trade'], 32)
        self.assertEqual(replay['candidates'], json.loads(json.dumps(first['candidates'])))
        c = first['candidates'][0]
        result = self.run_rows([uncalibrated_pair(i, c) for i in range(64)], NOW)
        self.assertEqual(result['profiles'][0]['kind'], 'SIZE_DOWN_UNCALIBRATED')
        revoked = self.run_rows([dict(rows[0], evidence_valid=False)], NOW)
        self.assertEqual(revoked['profiles'], [])
        self.assertTrue(revoked['lessons'][0]['quarantined'])

    def test_uncalibrated_training_requires_rr_costs_and_cross_channel_independence(self):
        pairs = []
        for i in range(16):
            direction = dict(observation(i, training=True, probability=None), net_reward_risk=1.2)
            pairs.extend([direction, uncalibrated_pair(i, training=True)])
        result = self.run_rows(pairs)
        self.assertEqual(result['lessons'][0]['uncalibrated_risk_observations'], 16)
        self.assertEqual(result['candidates'], [])
        result = self.run_rows([dict(uncalibrated_pair(16, training=True), net_reward_risk=None),
                                dict(uncalibrated_pair(17, training=True), costs_verified=False)])
        self.assertEqual(result['lessons'][0]['uncalibrated_risk_observations'], 16)
        self.assertEqual(result['candidates'], [])
        # Confidence is deliberately ignored; it cannot move a native event
        # into either of the probability-based experimental cohorts.
        result = self.run_rows([dict(uncalibrated_pair(i, training=True), confidence=.95) for i in range(18,34)])
        self.assertEqual(result['lessons'][0]['uncalibrated_risk_observations'], 32)
        self.assertEqual({c['kind'] for c in result['candidates']}, {'SIZE_DOWN_UNCALIBRATED'})
        self.assertLessEqual(len(result['candidates']), AL.MAX_CANDIDATES)

    def test_invalidated_historical_proof_revokes_derived_profiles(self):
        train = [observation(i, training=True) for i in range(32)]
        self.run_rows(train)
        result = self.run_rows([observation(i) for i in range(64)], NOW)
        self.assertTrue(result["profiles"])
        bad = dict(train[0], evidence_valid=False)
        result = self.run_rows([bad], NOW)
        self.assertFalse(result["profiles"])
        self.assertTrue(all(c["state"] == "degraded" for c in result["candidates"]))
        self.assertTrue(result["lessons"][0]["quarantined"])

    def test_publication_error_rolls_back_consumption_for_safe_retry(self):
        row = observation(0, training=True)
        with patch("veritas_learning_state.publish_snapshot_in_transaction", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "publication rejected"):
                self.run_rows([row])
        self.assertEqual(self.db.execute("SELECT COUNT(*) AS n FROM autonomous_learning_seen").fetchone()["n"], 0)
        self.assertEqual(self.run_rows([row])["counts"]["direction"], 1)

    def test_revocation_restarts_only_with_new_evidence_and_a_fresh_trial_epoch(self):
        train = [observation(i, training=True) for i in range(32)]
        self.run_rows(train)
        old = self.run_rows([observation(i) for i in range(64)], NOW)
        old_ids = {c["candidate_id"] for c in old["candidates"]}
        self.run_rows([dict(train[0], evidence_valid=False)], NOW)
        later = []
        for i in range(32):
            row = observation(200+i)
            decision = NOW+timedelta(hours=2*i+1)
            row.update(decision_at=decision.isoformat(), known_at=decision.isoformat(),
                       outcome_at=(decision+timedelta(hours=1)).isoformat(),
                       observed_at=(decision+timedelta(hours=1)).isoformat())
            later.append(row)
        result = self.run_rows(later, NOW+timedelta(days=3))
        fresh = [c for c in result["candidates"] if c["candidate_id"] not in old_ids]
        self.assertEqual(len(fresh), 2)
        self.assertTrue(all(c["training_n"] == 32 and c["training_epoch"] == 1 for c in fresh))
        self.assertTrue(all(c["state"] == "collecting" for c in fresh))
        self.assertEqual(result["lessons"][0]["independent_ideas"], 32)
        self.assertFalse(result["profiles"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM autonomous_learning_scope_archive").fetchone()["n"], 1)
        # Another correction to the invalidated epoch cannot revoke the newly
        # constructed, independently trained replacement experiment.
        result = self.run_rows([dict(train[1], evidence_valid=False)], NOW+timedelta(days=3))
        self.assertTrue(all(c["state"] == "collecting" for c in result["candidates"] if c["candidate_id"] not in old_ids))

    def test_new_cohort_gets_freed_candidate_capacity_before_old_cohort_renews(self):
        state = AL.initial_state()
        old_scope = AL.scope_for(observation(0))
        old_second = AL.scope_for(dict(observation(0), regime="ANOTHER_OLD_REGIME"))
        new_scope = AL.scope_for(dict(observation(0), regime="NEW_REGIME"))
        for scope, n, registrations in ((old_scope, 96, 2), (old_second, 96, 2), (new_scope, 32, 0)):
            state["scopes"][AL._digest(scope)] = {"scope": scope, "n": n, "correct": n,
                "probability_n": n, "residual_sum": 5., "evidence_hash": "prior",
                "registration_count": registrations,
                "registered_at_n": {"CALIBRATION": 32, "SIZE_DOWN_WEAK_SIGNAL": 32} if registrations else {}}
        # Fill the pool with completed trials. run_batch archives four terminal
        # records, then the fairness order must admit the never-tested cohort.
        for i in range(AL.MAX_CANDIDATES):
            c = AL.register_candidate(old_scope, {"n": 32, "residual_sum": 5.}, "CALIBRATION",
                                      CREATED-timedelta(days=i+1), trial_index=i+1)
            c["state"] = "rejected"
            state["candidates"][c["candidate_id"]] = c
        self.db.publish(self.db, AL.SNAPSHOT_NAME, AL.VERSION, state)
        self.db.db.commit()
        with patch.object(AL, "MAX_CANDIDATES", AL.MAX_CANDIDATES-3):
            result = self.run_rows([], NOW)
        active = [c for c in result["candidates"] if c["state"] not in AL.TERMINAL]
        self.assertTrue(any(c["scope"] == new_scope for c in active))

    def test_cold_cohort_restore_and_invalidation_preserve_learning_history(self):
        def row(index, regime):
            return dict(observation(index, training=True), regime=regime)
        original = row(0, "OLD")
        with patch.object(AL, "MAX_SCOPES", 2):
            result = self.run_rows([original, row(1, "SECOND"), row(2, "THIRD")])
            self.assertEqual(result["hot_scope_count"], 2)
            self.assertEqual(result["counts"]["cold_scopes"], 1)
            result = self.run_rows([row(3, "OLD")])
            old = next(s for s in result["lessons"] if s["scope"]["regime"] == "OLD")
            self.assertEqual(old["independent_ideas"], 2)
            self.assertEqual(result["counts"]["direction"], 4)
            # SECOND is now cold; revoke its original source record anyway.
            result = self.run_rows([dict(row(1, "SECOND"), evidence_valid=False)])
            second = next(s for s in result["lessons"] if s["scope"]["regime"] == "SECOND")
            self.assertTrue(second["quarantined"])
            self.assertEqual(second["independent_ideas"], 1)
            self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM autonomous_learning_scope_archive").fetchone()["n"], 1)

    def test_budget_abort_rolls_back_whole_microbatch(self):
        class Budget:
            calls = 0
            def check(self):
                self.calls += 1
                if self.calls == 2:
                    raise TimeoutError("budget exhausted")
        with self.assertRaises(TimeoutError):
            self.run_rows([observation(i, training=True) for i in range(2)], context=Budget())
        self.assertEqual(self.db.execute("SELECT COUNT(*) AS n FROM autonomous_learning_seen").fetchone()["n"], 0)

    def test_batch_is_bounded_and_history_is_not_embedded_in_snapshot(self):
        with self.assertRaises(ValueError):
            self.run_rows([observation(0)]*257)
        result = self.run_rows([observation(i, training=True) for i in range(32)])
        self.assertNotIn("observations", result)
        self.assertLess(len(json.dumps(result)), 20000)


if __name__ == "__main__":
    unittest.main()
