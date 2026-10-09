from datetime import datetime, timedelta, timezone
import inspect
import unittest

import veritas_learning_v2 as L2
import veritas_learning_v2_registry as R

T0=datetime(2026,10,1,tzinfo=timezone.utc)

class LearningV2RegistryTests(unittest.TestCase):
    def entry_candidate(self):
        scope={"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p","source_key":"S"}
        return {"kind":"ENTRY_BLOCKER_RELAXATION","scope":scope,
                "proposal":{"blocker":"IMPULSE_ALREADY_PASSED"},
                "registered_at":T0.isoformat()}

    def decision(self, at, fr=.01, family="BREAKOUT", direction="LONG"):
        return {"event_ts":at.isoformat(),"asset":"NQ","horizon":"5m","regime":"TREND",
                "policy_hash":"p","source_key":"S","decision":"NO_TRADE","candidate_direction":direction,
                "final_gate_blockers":["IMPULSE_ALREADY_PASSED"],"forward_return":fr,
                "setup_family":family}

    def test_entry_uses_only_post_registration_observations(self):
        before=[self.decision(T0-timedelta(days=1),.02) for _ in range(100)]
        future=[]
        for i in range(64):
            future.append(self.decision(T0+timedelta(days=1+(i%16),minutes=i),.01))
        result=R.evaluate_candidate(self.entry_candidate(),before+future,[],T0+timedelta(days=20))
        self.assertEqual(result["prospective"]["n"],64)
        self.assertEqual(result["status"],"SHADOW_ELIGIBLE")
        self.assertFalse(result["prospective"]["execution_pnl_proven"])

    def test_incremental_checkpoint_does_not_double_count_same_decisions(self):
        first=[]
        second=[]
        for i in range(64):
            row=self.decision(T0+timedelta(days=1+(i%16),minutes=i),.01)
            row["decision_id"]=101+i
            (first if i<32 else second).append(row)
        r1=R.evaluate_candidate(self.entry_candidate(),first,[],T0+timedelta(days=20),cutoff_id=100)
        self.assertEqual(r1["prospective"]["n"],32)
        r2=R.evaluate_candidate(self.entry_candidate(),first+second,[],T0+timedelta(days=20),
                                prior=r1["prospective"],cutoff_id=100)
        self.assertEqual(r2["prospective"]["n"],64)
        self.assertEqual(r2["prospective"]["new_observations"],32)
        self.assertEqual(r2["prospective"]["last_decision_id"],164)
        self.assertEqual(r2["status"],"SHADOW_ELIGIBLE")

    def test_directional_label_with_blocker_can_validate_entry(self):
        rows=[]
        for i in range(64):
            row=self.decision(T0+timedelta(days=1+(i%16),minutes=i),.01)
            row["decision"]="LONG"
            row["admission_eligible"]=False
            rows.append(row)
        result=R.evaluate_candidate(self.entry_candidate(),rows,[],T0+timedelta(days=20))
        self.assertEqual(result["status"],"SHADOW_ELIGIBLE")
        self.assertEqual(result["prospective"]["n"],64)

    def test_no_direction_or_move_against_candidate_cannot_validate_entry(self):
        rows=[]
        for i in range(80):
            row=self.decision(T0+timedelta(days=1+(i%16),minutes=i),-.01)
            rows.append(row)
        result=R.evaluate_candidate(self.entry_candidate(),rows,[],T0+timedelta(days=20))
        self.assertNotEqual(result["status"],"SHADOW_ELIGIBLE")
        self.assertLessEqual(result["prospective"]["favourable_rate"],.05)

    def test_router_requires_future_preferred_outperformance(self):
        candidate={"kind":"STRATEGY_ROUTER",
                   "scope":{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"*","source_key":"S"},
                   "proposal":{"preferred_family":"BREAKOUT"},
                   "registered_at":T0.isoformat()}
        rows=[]
        for i in range(60):
            at=T0+timedelta(days=1+(i%16),minutes=i)
            rows.append({"event_ts":at.isoformat(),"asset":"NQ","horizon":"5m","regime":"TREND",
                         "policy_hash":"p","source_key":"S","decision":"LONG","forward_return":.01 if i<48 else -.01,
                         "setup_family":"BREAKOUT"})
            rows.append({"event_ts":at.isoformat(),"asset":"NQ","horizon":"5m","regime":"TREND",
                         "policy_hash":"p","source_key":"S","decision":"LONG","forward_return":.01 if i<30 else -.01,
                         "setup_family":"TREND"})
        result=R.evaluate_candidate(candidate,rows,[],T0+timedelta(days=20))
        self.assertEqual(result["status"],"SHADOW_ELIGIBLE")
        self.assertGreaterEqual(result["prospective"]["hit_rate_delta"],.05)
        self.assertFalse(result["prospective"]["causal_superiority_proven"])

    def test_router_excludes_other_source(self):
        candidate={"kind":"STRATEGY_ROUTER",
                   "scope":{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"*","source_key":"S"},
                   "proposal":{"preferred_family":"BREAKOUT"},
                   "registered_at":T0.isoformat()}
        rows=[]
        for i in range(60):
            at=T0+timedelta(days=1+(i%16),minutes=i)
            rows.append({"event_ts":at.isoformat(),"asset":"NQ","horizon":"5m","regime":"TREND",
                         "policy_hash":"p","source_key":"S","decision":"LONG","forward_return":.01,
                         "setup_family":"BREAKOUT"})
            rows.append({"event_ts":at.isoformat(),"asset":"NQ","horizon":"5m","regime":"TREND",
                         "policy_hash":"p","source_key":"OTHER","decision":"LONG","forward_return":-.01,
                         "setup_family":"TREND"})
        result=R.evaluate_candidate(candidate,rows,[],T0+timedelta(days=20))
        self.assertEqual(result["prospective"]["preferred_n"],60)
        self.assertEqual(result["prospective"]["other_n"],0)
        self.assertNotEqual(result["status"],"SHADOW_ELIGIBLE")

    def test_stop_and_exit_wait_for_ordered_path_replay(self):
        for kind in ("STOP_GEOMETRY","EXIT_CAPTURE"):
            candidate={"kind":kind,"scope":{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p"},
                       "proposal":{},"registered_at":T0.isoformat()}
            result=R.evaluate_candidate(candidate,[],[],T0+timedelta(days=20))
            self.assertEqual(result["status"],"AWAIT_REPLAY")
            self.assertEqual(result["prospective"]["reason"],"ORDERED_PATH_REPLAY_REQUIRED")

    def test_replay_support_requires_positive_candidate_and_delta(self):
        candidate={"kind":"STOP_GEOMETRY",
                   "scope":{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p"},
                   "proposal":{"stop_buffer_atr":.2},"registered_at":T0.isoformat()}
        prior={"replay":{"n":32,"utc_days":[f"2026-10-{i:02d}" for i in range(1,9)],
                         "days":8,"sum_baseline":.16,"sum_candidate":.32,
                         "ambiguous":2,"invalid":1}}
        result=R.evaluate_candidate(candidate,[],[],T0+timedelta(days=20),prior=prior)
        self.assertEqual(result["status"],"REPLAY_SUPPORTED")
        self.assertGreater(result["prospective"]["replay"]["mean_delta_net_return"],0)
        self.assertFalse(result["prospective"]["replay"]["counterfactual_live_execution_proven"])

    def test_replay_rejects_persistent_nonpositive_delta(self):
        candidate={"kind":"EXIT_CAPTURE",
                   "scope":{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p"},
                   "proposal":{"first_target_fraction":.25},"registered_at":T0.isoformat()}
        prior={"replay":{"n":64,"utc_days":[f"2026-10-{i:02d}" for i in range(1,15)],
                         "days":14,"sum_baseline":.64,"sum_candidate":.32,
                         "ambiguous":2,"invalid":0}}
        result=R.evaluate_candidate(candidate,[],[],T0+timedelta(days=20),prior=prior)
        self.assertEqual(result["status"],"REJECTED")

    def test_high_replay_ambiguity_cannot_be_supported(self):
        candidate={"kind":"STOP_GEOMETRY",
                   "scope":{"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p"},
                   "proposal":{"stop_buffer_atr":.2},"registered_at":T0.isoformat()}
        prior={"replay":{"n":32,"utc_days":[f"2026-10-{i:02d}" for i in range(1,9)],
                         "days":8,"sum_baseline":.16,"sum_candidate":.32,
                         "ambiguous":8,"invalid":0}}
        result=R.evaluate_candidate(candidate,[],[],T0+timedelta(days=20),prior=prior)
        self.assertEqual(result["status"],"REPLAY_BUILDING")

    def test_sync_query_is_bounded_to_active_asset_when_snapshot_has_asset(self):
        source=inspect.getsource(R.sync)
        self.assertIn("WHERE version=%s AND asset=%s",source)
        self.assertIn("learning_v2_registry_asset_status",inspect.getsource(R.ensure_schema))

    def test_hypothesis_id_is_stable_when_training_evidence_grows(self):
        scope={"asset":"NQ","horizon":"5m","regime":"TREND","policy_hash":"p"}
        a=L2._hypothesis("ENTRY_BLOCKER_RELAXATION",scope,{"blocker":"X"},{"n":24})
        b=L2._hypothesis("ENTRY_BLOCKER_RELAXATION",scope,{"blocker":"X"},{"n":240})
        self.assertEqual(a["hypothesis_id"],b["hypothesis_id"])
        self.assertNotEqual(a["evidence"],b["evidence"])

if __name__=="__main__":
    unittest.main()
