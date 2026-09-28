from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

VERSION = "veritas-model-promotion-v1"


@dataclass(frozen=True)
class PromotionEvidence:
    model_version: str
    oos_n: int
    oos_expectancy: float
    oos_profit_factor: float
    vault_n: int
    vault_expectancy: float
    vault_profit_factor: float
    high_cost_expectancy: float
    calibration_n: int
    ece: Optional[float]
    shadow_trades: int
    shadow_expectancy: float
    shadow_max_drawdown: float
    code_ci_pass: bool
    data_parity_pass: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def promotion_gate(e: PromotionEvidence) -> Dict[str, Any]:
    blockers=[]
    min_oos=int(os.getenv("VERITAS_PROMOTION_MIN_OOS_N","100"))
    min_vault=int(os.getenv("VERITAS_PROMOTION_MIN_VAULT_N","50"))
    min_cal=int(os.getenv("VERITAS_PROMOTION_MIN_CALIBRATION_N","100"))
    min_shadow=int(os.getenv("VERITAS_PROMOTION_MIN_SHADOW_TRADES","50"))
    max_ece=float(os.getenv("VERITAS_PROMOTION_MAX_ECE","0.10"))
    max_dd=float(os.getenv("VERITAS_PROMOTION_MAX_SHADOW_DRAWDOWN","0.10"))

    if not e.code_ci_pass: blockers.append("CI_NOT_PASSING")
    if not e.data_parity_pass: blockers.append("DATA_PARITY_NOT_PASSING")
    if e.oos_n < min_oos: blockers.append("OOS_SAMPLE_TOO_SMALL")
    if e.oos_expectancy <= 0: blockers.append("OOS_EXPECTANCY_NOT_POSITIVE")
    if e.oos_profit_factor < 1.10: blockers.append("OOS_PROFIT_FACTOR_TOO_LOW")
    if e.vault_n < min_vault: blockers.append("VAULT_SAMPLE_TOO_SMALL")
    if e.vault_expectancy <= 0: blockers.append("VAULT_EXPECTANCY_NOT_POSITIVE")
    if e.vault_profit_factor < 1.05: blockers.append("VAULT_PROFIT_FACTOR_TOO_LOW")
    if e.high_cost_expectancy <= 0: blockers.append("HIGH_COST_STRESS_NOT_POSITIVE")
    if e.calibration_n < min_cal: blockers.append("CALIBRATION_SAMPLE_TOO_SMALL")
    if e.ece is None or e.ece > max_ece: blockers.append("CALIBRATION_ECE_TOO_HIGH_OR_MISSING")
    if e.shadow_trades < min_shadow: blockers.append("SHADOW_SAMPLE_TOO_SMALL")
    if e.shadow_expectancy <= 0: blockers.append("SHADOW_EXPECTANCY_NOT_POSITIVE")
    if e.shadow_max_drawdown > max_dd: blockers.append("SHADOW_DRAWDOWN_TOO_HIGH")

    ok=not blockers
    return {
        "eligible_for_production":ok,
        "status":"PASS" if ok else "BLOCK",
        "blockers":blockers,
        "model_version":e.model_version,
        "evidence":e.to_dict(),
        "thresholds":{
            "min_oos_n":min_oos,"min_vault_n":min_vault,
            "min_calibration_n":min_cal,"min_shadow_trades":min_shadow,
            "max_ece":max_ece,"max_shadow_drawdown":max_dd,
        },
        "principle":"Training may propose a candidate; only independent evidence can promote it.",
        "version":VERSION,
    }
