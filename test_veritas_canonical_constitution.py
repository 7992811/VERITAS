import unittest

import veritas_canonical_constitution as C


class CanonicalConstitutionRegistryTests(unittest.TestCase):
    def test_registry_is_self_consistent(self):
        self.assertTrue(C.validate_constitution())
        self.assertEqual(C.VERSION, "CTC_V2_2026_10_06")
        self.assertEqual(len(C.CANONICAL_RULES), 60)
        self.assertEqual(len({x["id"] for x in C.CANONICAL_RULES}), 60)
        self.assertFalse(set(C.HARD_VETOES) & set(C.SOFT_VETOES))

    def test_stage_order_is_fixed(self):
        self.assertEqual(
            C.STAGE_ORDER,
            ("DATA","THESIS","TIMING","ECONOMICS","RISK","SIZE","LIFECYCLE","LEARNING"),
        )

    def test_objective_reconciles_r35_and_r40(self):
        self.assertEqual(C.OBJECTIVE_POLICY["target_win_rate"], 0.65)
        self.assertEqual(
            C.OBJECTIVE_POLICY["priority_order"][0],
            "SUSTAINABLE_HIGH_WIN_RATE",
        )
        self.assertIn("POSITIVE_POST_COST", C.OBJECTIVE_POLICY["hard_constraint"])

    def test_cost_arithmetic(self):
        self.assertAlmostEqual(C.COST_POLICY["commission_rate_per_side"], 0.0004)
        self.assertAlmostEqual(C.COST_POLICY["slippage_rate_per_side"], 0.0004)
        self.assertAlmostEqual(C.COST_POLICY["round_trip_base_cost_pct"], 0.0016)
        self.assertAlmostEqual(C.COST_POLICY["cost_buffer_multiple"], 1.1)
        self.assertAlmostEqual(C.COST_POLICY["entry_cost_multiple"], 1.1)
        self.assertAlmostEqual(
            C.COST_POLICY["round_trip_base_cost_pct"] * C.COST_POLICY["cost_buffer_multiple"],
            0.00176,
        )


class CanonicalConstitutionCurrentRuntimeAlignmentTests(unittest.TestCase):
    def test_current_cost_module_matches_canonical_values(self):
        import veritas_costs as VC
        self.assertAlmostEqual(VC.COMMISSION_RATE, C.COST_POLICY["commission_rate_per_side"])
        self.assertAlmostEqual(VC.SLIPPAGE_RATE, C.COST_POLICY["slippage_rate_per_side"])
        self.assertAlmostEqual(VC.COST_BUFFER_MULTIPLE, C.COST_POLICY["cost_buffer_multiple"])
        self.assertAlmostEqual(VC.ROUND_TRIP_RATE, C.COST_POLICY["round_trip_base_cost_pct"])
        self.assertAlmostEqual(VC.FUNDING_ANNUAL_RATE, C.COST_POLICY["funding_annual_rate"])
        self.assertEqual(VC.FUNDING_FREE_SECONDS, C.COST_POLICY["funding_free_seconds"])

    def test_minimum_move_formula_matches_execution_quality(self):
        import veritas_costs as VC
        import veritas_execution as VX
        self.assertAlmostEqual(VX.MIN_EXPECTED_MOVE_PCT, 0.0019)
        self.assertAlmostEqual(
            VX.minimum_expected_move_pct(VC.ROUND_TRIP_RATE),
            max(0.0019, 1.1 * 0.0016),
        )

    def test_ctc_v2_runtime_authority_is_explicit(self):
        import veritas_portfolio as VP
        import veritas_portfolio_runtime as VPR
        self.assertEqual(VPR.FINAL_RUNTIME_AUTHORITY_VERSION, C.BASIS_RUNTIME)
        self.assertIs(VP._signal_first_admission, VPR.FINAL_SIGNAL_FIRST_ADMISSION)
        self.assertIs(VP._open_or_add, VPR.FINAL_OPEN_OR_ADD)
        self.assertIs(VP._close_or_reduce, VPR.FINAL_CLOSE_OR_REDUCE)
        self.assertIs(VP._step_one, VPR.FINAL_STEP_ONE)
        self.assertIs(VP.step_all, VPR.FINAL_STEP_ALL)

    def test_cost_negative_blockers_are_hard_not_soft(self):
        import veritas_portfolio as VP
        VPR = VP._VERITAS_RUNTIME
        self.assertNotIn("RR_BELOW_FINAL_FLOOR", VPR._R79_SOFT_ECON_BLOCKERS)
        self.assertEqual(C.veto_severity("RR_BELOW_FINAL_FLOOR"), "HARD")
        self.assertNotIn("NET_REWARD_RISK_BELOW_FLOOR", VPR._R79_SOFT_ECON_BLOCKERS)
        self.assertEqual(C.veto_severity("NET_REWARD_RISK_BELOW_FLOOR"), "HARD")
        self.assertNotIn("EXPECTED_MOVE_BELOW_COST_BUFFER", VPR._R79_SOFT_ECON_BLOCKERS)
        self.assertNotIn("TARGET_NOT_PROFITABLE_AFTER_COSTS", VPR._R79_SOFT_ECON_BLOCKERS)
        self.assertIn("EXPECTED_MOVE_BELOW_COST_BUFFER", VPR._R79_HARD_COST_BLOCKERS)
        self.assertIn("TARGET_NOT_PROFITABLE_AFTER_COSTS", VPR._R79_HARD_COST_BLOCKERS)

    def test_live_profile_is_stricter_than_research_aggressive(self):
        import veritas_execution as VX
        self.assertEqual(VX.LIVE_RISK_PROFILE["max_stop_risk_nav"], C.LIVE_RISK_POLICY["max_stop_risk_nav"])
        self.assertEqual(VX.LIVE_RISK_PROFILE["max_gross"], C.LIVE_RISK_POLICY["max_gross"])
        self.assertLess(C.LIVE_RISK_POLICY["max_gross"], C.PORTFOLIO_POLICIES["Aggressive"]["max_gross"])


class CanonicalConstitutionDeclaredGapsTests(unittest.TestCase):
    def test_resolved_gap_registry_is_closed(self):
        self.assertEqual(C.IMPLEMENTATION_GAPS, [])
        self.assertEqual({x["id"] for x in C.RESOLVED_IMPLEMENTATION_GAPS},
                         {f"GAP{i:02d}" for i in range(1,19)})

    def test_core_caps_match_canonical(self):
        import veritas_portfolio as VP
        self.assertEqual(VP.POLICIES["Champion"]["max_fraction"], 1.0)
        self.assertEqual(VP.POLICIES["Challenger"]["max_fraction"], 1.0)
        self.assertEqual(VP.POLICIES["Impulse"]["max_gross"], 0.50)
        for name in C.PORTFOLIO_ORDER:
            self.assertEqual(VP.POLICIES[name], C.runtime_portfolio_policy(name))

    def test_currency_owner_policy_is_active(self):
        import veritas_currency_portfolio as VCP
        runtime = VCP.policy()
        canonical = C.PORTFOLIO_POLICIES["Currency"]
        self.assertEqual(runtime["configuration_status"], "CONFIGURED")
        self.assertEqual(runtime["initial_nav_rub"], 10_000.0)
        self.assertEqual(runtime["max_gross"], 10.0)
        self.assertEqual(runtime["hard_drawdown"], 0.35)
        self.assertTrue(runtime["weekend_carry_allowed"])
        self.assertEqual(canonical["runtime_status"], "CONFIGURED_PAPER")


if __name__ == "__main__":
    unittest.main()
