import unittest

import veritas_canonical_constitution as C


class CanonicalConstitutionRegistryTests(unittest.TestCase):
    def test_registry_is_self_consistent(self):
        self.assertTrue(C.validate_constitution())
        self.assertEqual(C.VERSION, "CTC_V1_2026_10_06")
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
        self.assertAlmostEqual(C.COST_POLICY["commission_rate_per_side"], 0.0005)
        self.assertAlmostEqual(C.COST_POLICY["slippage_rate_per_side"], 0.0004)
        self.assertAlmostEqual(C.COST_POLICY["round_trip_base_cost_pct"], 0.0018)
        self.assertAlmostEqual(C.COST_POLICY["cost_buffer_multiple"], 1.2)
        self.assertAlmostEqual(
            C.COST_POLICY["round_trip_base_cost_pct"] * C.COST_POLICY["cost_buffer_multiple"],
            0.00216,
        )


class CanonicalConstitutionCurrentR85AlignmentTests(unittest.TestCase):
    def test_current_cost_module_matches_canonical_values(self):
        import veritas_costs as VC
        self.assertAlmostEqual(VC.COMMISSION_RATE, C.COST_POLICY["commission_rate_per_side"])
        self.assertAlmostEqual(VC.SLIPPAGE_RATE, C.COST_POLICY["slippage_rate_per_side"])
        self.assertAlmostEqual(VC.COST_BUFFER_MULTIPLE, C.COST_POLICY["cost_buffer_multiple"])
        self.assertAlmostEqual(VC.ROUND_TRIP_RATE, C.COST_POLICY["round_trip_base_cost_pct"])
        self.assertAlmostEqual(VC.FUNDING_ANNUAL_RATE, C.COST_POLICY["funding_annual_rate"])
        self.assertEqual(VC.FUNDING_FREE_SECONDS, C.COST_POLICY["funding_free_seconds"])

    def test_minimum_move_formula_matches_r85_execution(self):
        import veritas_costs as VC
        import veritas_execution as VX
        self.assertAlmostEqual(VX.MIN_EXPECTED_MOVE_PCT, 0.0019)
        self.assertAlmostEqual(
            VX.minimum_expected_move_pct(VC.ROUND_TRIP_RATE),
            max(0.0019, 1.2 * 0.0018),
        )

    def test_r85_runtime_authority_is_explicit(self):
        import veritas_portfolio as VP
        import veritas_portfolio_runtime as VPR
        self.assertEqual(VPR.FINAL_RUNTIME_AUTHORITY_VERSION, C.BASIS_RUNTIME)
        self.assertIs(VP._signal_first_admission, VPR.FINAL_SIGNAL_FIRST_ADMISSION)
        self.assertIs(VP._open_or_add, VPR.FINAL_OPEN_OR_ADD)
        self.assertIs(VP._close_or_reduce, VPR.FINAL_CLOSE_OR_REDUCE)
        self.assertIs(VP._step_one, VPR.FINAL_STEP_ONE)
        self.assertIs(VP.step_all, VPR.FINAL_STEP_ALL)

    def test_cost_negative_blockers_are_hard_not_soft(self):
        import veritas_portfolio_runtime as VPR
        self.assertIn("RR_BELOW_FINAL_FLOOR", VPR._R79_SOFT_ECON_BLOCKERS)
        self.assertIn("NET_REWARD_RISK_BELOW_FLOOR", VPR._R79_SOFT_ECON_BLOCKERS)
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
    def _gap(self, gap_id):
        return next(x for x in C.IMPLEMENTATION_GAPS if x["id"] == gap_id)

    def test_champion_runtime_gap_is_explicit(self):
        import veritas_portfolio as VP
        self.assertEqual(VP.POLICIES["Champion"]["max_fraction"], 2.0)
        self.assertEqual(C.PORTFOLIO_POLICIES["Champion"]["max_single_asset_fraction"], 1.0)
        self.assertEqual(self._gap("GAP01")["severity"], "HIGH")

    def test_challenger_runtime_gap_is_explicit(self):
        import veritas_portfolio as VP
        self.assertEqual(VP.POLICIES["Challenger"]["max_fraction"], 2.0)
        self.assertEqual(C.PORTFOLIO_POLICIES["Challenger"]["max_single_asset_fraction"], 1.0)
        self.assertEqual(self._gap("GAP02")["severity"], "HIGH")

    def test_currency_owner_policy_is_recorded_but_runtime_still_pending(self):
        import veritas_currency_portfolio as VCP
        runtime = VCP.policy()
        canonical = C.PORTFOLIO_POLICIES["Currency"]
        self.assertEqual(runtime["configuration_status"], "SETUP_PENDING")
        self.assertEqual(runtime["initial_nav_rub"], 0.0)
        self.assertEqual(canonical["initial_nav_rub"], 10_000.0)
        self.assertEqual(canonical["max_gross"], 10.0)
        self.assertEqual(canonical["hard_drawdown"], 0.35)
        self.assertTrue(canonical["weekend_carry_allowed"])
        self.assertEqual(self._gap("GAP03")["severity"], "HIGH")

    def test_current_documentation_gap_is_declared(self):
        gap = self._gap("GAP06")
        self.assertEqual(gap["area"], "documentation")
        self.assertIn("README_R82", gap["current"])


if __name__ == "__main__":
    unittest.main()
