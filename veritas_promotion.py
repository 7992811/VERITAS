from __future__ import annotations

import os
import math
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

VERSION = "veritas-model-promotion-v2"


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


def _finite_number(value):
    # bool is an int subclass; typed evidence must not coerce strings or flags.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _json_safe(value):
    """Keep a blocked evidence report serializable without presenting NaN as data."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return None


def _configuration():
    thresholds = {}
    invalid = []
    for key, env, default in (
        ("min_oos_n", "VERITAS_PROMOTION_MIN_OOS_N", "100"),
        ("min_vault_n", "VERITAS_PROMOTION_MIN_VAULT_N", "50"),
        ("min_calibration_n", "VERITAS_PROMOTION_MIN_CALIBRATION_N", "100"),
        ("min_shadow_trades", "VERITAS_PROMOTION_MIN_SHADOW_TRADES", "50"),
    ):
        try:
            value = int(os.getenv(env, default))
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is None or value < 1:
            invalid.append(env)
            value = None
        thresholds[key] = value
    for key, env, default in (
        ("max_ece", "VERITAS_PROMOTION_MAX_ECE", "0.10"),
        ("max_shadow_drawdown", "VERITAS_PROMOTION_MAX_SHADOW_DRAWDOWN", "0.10"),
    ):
        try:
            value = _finite_number(float(os.getenv(env, default)))
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is None or not 0 <= value <= 1:
            invalid.append(env)
            value = None
        thresholds[key] = value
    return thresholds, invalid


def promotion_gate(e: PromotionEvidence) -> Dict[str, Any]:
    """Validate evidence before thresholds; eligibility never applies a model."""
    invalid = []
    for field in ("oos_n", "vault_n", "calibration_n", "shadow_trades"):
        value = getattr(e, field)
        if type(value) is not int or value < 0:
            invalid.append(field)
    for field in ("oos_expectancy", "oos_profit_factor", "vault_expectancy",
                  "vault_profit_factor", "high_cost_expectancy", "ece",
                  "shadow_expectancy", "shadow_max_drawdown"):
        value = _finite_number(getattr(e, field))
        bad_domain = (
            value is not None and
            ((field.endswith("profit_factor") and value < 0) or
             (field in ("ece", "shadow_max_drawdown") and not 0 <= value <= 1))
        )
        if value is None or bad_domain:
            invalid.append(field)
    if not isinstance(e.model_version, str) or not e.model_version.strip():
        invalid.append("model_version")
    for field in ("code_ci_pass", "data_parity_pass"):
        if type(getattr(e, field)) is not bool:
            invalid.append(field)

    thresholds, invalid_config = _configuration()
    blockers = ["INVALID_EVIDENCE_" + field.upper() for field in invalid]
    blockers += ["INVALID_CONFIGURATION_" + field for field in invalid_config]
    result = {
        "eligible_for_production": False,
        "status": "BLOCK",
        "blockers": blockers,
        "model_version": _json_safe(e.model_version),
        "evidence": _json_safe(e.to_dict()),
        "thresholds": thresholds,
        "invalid_evidence_fields": invalid,
        "invalid_configuration_fields": invalid_config,
        "automatic_promotion": False,
        "principle": "Training may propose a candidate; only independent evidence can promote it.",
        "version": VERSION,
    }
    if invalid or invalid_config:
        return result

    if not e.code_ci_pass: blockers.append("CI_NOT_PASSING")
    if not e.data_parity_pass: blockers.append("DATA_PARITY_NOT_PASSING")
    if e.oos_n < thresholds["min_oos_n"]: blockers.append("OOS_SAMPLE_TOO_SMALL")
    if e.oos_expectancy <= 0: blockers.append("OOS_EXPECTANCY_NOT_POSITIVE")
    if e.oos_profit_factor < 1.10: blockers.append("OOS_PROFIT_FACTOR_TOO_LOW")
    if e.vault_n < thresholds["min_vault_n"]: blockers.append("VAULT_SAMPLE_TOO_SMALL")
    if e.vault_expectancy <= 0: blockers.append("VAULT_EXPECTANCY_NOT_POSITIVE")
    if e.vault_profit_factor < 1.05: blockers.append("VAULT_PROFIT_FACTOR_TOO_LOW")
    if e.high_cost_expectancy <= 0: blockers.append("HIGH_COST_STRESS_NOT_POSITIVE")
    if e.calibration_n < thresholds["min_calibration_n"]: blockers.append("CALIBRATION_SAMPLE_TOO_SMALL")
    if e.ece > thresholds["max_ece"]: blockers.append("CALIBRATION_ECE_TOO_HIGH_OR_MISSING")
    if e.shadow_trades < thresholds["min_shadow_trades"]: blockers.append("SHADOW_SAMPLE_TOO_SMALL")
    if e.shadow_expectancy <= 0: blockers.append("SHADOW_EXPECTANCY_NOT_POSITIVE")
    if e.shadow_max_drawdown > thresholds["max_shadow_drawdown"]: blockers.append("SHADOW_DRAWDOWN_TOO_HIGH")
    result["eligible_for_production"] = not blockers
    result["status"] = "BLOCK" if blockers else "PASS"
    return result
