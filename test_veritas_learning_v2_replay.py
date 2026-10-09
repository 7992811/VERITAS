from datetime import datetime, timedelta, timezone
import unittest
import veritas_learning_v2_replay as R

T=datetime(2026,10,8,10,tzinfo=timezone.utc)

def bar(i,o,h,l,c,source="S"):
    return {"closed_at":(T+timedelta(minutes=5*i)).isoformat(),
            "open":o,"high":h,"low":l,"close":c,"source_key":source}

class LearningV2ReplayTests(unittest.TestCase):
    def test_same_bar_stop_and_target_is_excluded(self):
        rows=[bar(1,100,103,97,101)]
        z=R.replay_stop_target(rows,entry_at=T,entry_price=100,direction="LONG",
                               stop_price=98,target_price=102,source_key="S")
        self.assertEqual(z["status"],R.AMBIGUOUS)

    def test_ordered_target_then_later_stop_is_resolved_causally(self):
        rows=[bar(1,100,102.1,99.5,101.8),bar(2,101.8,102,97.5,98)]
        z=R.replay_stop_target(rows,entry_at=T,entry_price=100,direction="LONG",
                               stop_price=98,target_price=102,source_key="S",cost_per_side=.0004)
        self.assertEqual(z["exit_reason"],"TARGET")
        self.assertAlmostEqual(z["net_return"],.02-.0008,places=10)

    def test_partial_entry_bar_is_excluded(self):
        rows=[
            {"opened_at":T.isoformat(),"closed_at":(T+timedelta(minutes=5)).isoformat(),
             "open":100,"high":110,"low":90,"close":100,"source_key":"S","contract_id":"C"},
            {"opened_at":(T+timedelta(minutes=5)).isoformat(),"closed_at":(T+timedelta(minutes=10)).isoformat(),
             "open":100,"high":101,"low":99,"close":100.5,"source_key":"S","contract_id":"C"},
        ]
        z=R.replay_stop_target(rows,entry_at=T+timedelta(minutes=2),entry_price=100,direction="LONG",
                               stop_price=98,target_price=102,source_key="S",contract_id="C")
        self.assertEqual(z["status"],"OPEN_AT_END")
        self.assertEqual(z["bars_used"],1)

    def test_contract_mismatch_invalidates_replay(self):
        rows=[{"opened_at":(T+timedelta(minutes=5)).isoformat(),
               "closed_at":(T+timedelta(minutes=10)).isoformat(),
               "open":100,"high":101,"low":99,"close":100.5,
               "source_key":"S","contract_id":"OTHER"}]
        z=R.replay_stop_target(rows,entry_at=T,entry_price=100,direction="LONG",
                               stop_price=98,target_price=102,source_key="S",contract_id="C")
        self.assertEqual(z["status"],R.INVALID)
        self.assertEqual(z["reason"],"CONTRACT_ID_MISMATCH")

    def test_source_mismatch_invalidates_replay(self):
        z=R.replay_stop_target([bar(1,100,101,99,100.5,"OTHER")],
                               entry_at=T,entry_price=100,direction="LONG",
                               stop_price=98,target_price=102,source_key="S")
        self.assertEqual(z["status"],R.INVALID)
        self.assertEqual(z["reason"],"SOURCE_IDENTITY_MISMATCH")

    def test_structural_stop_buffers_match_long_and_short_geometry(self):
        self.assertAlmostEqual(R.structural_stop(99,2,"LONG",.15),98.7)
        self.assertAlmostEqual(R.structural_stop(101,2,"SHORT",.15),101.3)

    def test_partial_runner_never_assumes_intrabar_order(self):
        rows=[bar(1,100,102.2,99.5,102),bar(2,102,104.2,97.8,101)]
        z=R.replay_partial_runner(rows,entry_at=T,entry_price=100,direction="LONG",
            stop_price=98,first_target=102,runner_target=104,first_fraction=.5,source_key="S")
        self.assertEqual(z["status"],R.AMBIGUOUS)
        self.assertEqual(z["reason"],"STOP_AND_RUNNER_TARGET_SAME_BAR")

    def test_partial_runner_resolves_when_bar_order_is_known(self):
        rows=[bar(1,100,102.2,99.5,102),bar(2,102,103,100,102.5),bar(3,102.5,104.2,102,104)]
        z=R.replay_partial_runner(rows,entry_at=T,entry_price=100,direction="LONG",
            stop_price=98,first_target=102,runner_target=104,first_fraction=.5,source_key="S")
        self.assertEqual(z["status"],"RESOLVED")
        self.assertEqual(z["exit_reason"],"RUNNER_TARGET")
        self.assertAlmostEqual(z["gross_return"],.03,places=10)

if __name__=="__main__":
    unittest.main()
