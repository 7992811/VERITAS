from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import veritas_execution as VX
import veritas_instruments as VI
import veritas_promotion as VPROM
import veritas_risk as VR

VERSION="veritas-live-authorization-v1"


@dataclass(frozen=True)
class LiveCandidate:
    asset: str
    direction: str
    fraction_nav: float
    entry_price: float
    stop_price: float
    gross_after: float
    drawdown: float
    calibrated_probability: float
    model_version: str
    plan: Dict[str, Any]
    source_gate: Dict[str, Any]
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0


def _armed() -> Dict[str, Any]:
    enabled=os.getenv("VERITAS_LIVE_EXECUTION_ENABLED","0").lower() in ("1","true","yes","on")
    armed=os.getenv("VERITAS_LIVE_EXECUTION_ARMED","0").lower() in ("1","true","yes","on")
    return {"enabled":enabled,"armed":armed,"ready":enabled and armed}


def authorize_candidate(candidate: LiveCandidate,
                        current_positions: Iterable[VR.PositionRisk],
                        correlations: Optional[Dict[str, Dict[str, float]]],
                        instrument_registry: VI.InstrumentRegistry,
                        promotion_evidence: VPROM.PromotionEvidence,
                        durable_storage: bool,
                        broker_reconciled: bool,
                        kill_switch: bool = False,
                        risk_profile: Optional[Dict[str, Any]] = None,
                        probability_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    proposed=VR.PositionRisk(
        asset=candidate.asset,
        direction=candidate.direction,
        fraction_nav=candidate.fraction_nav,
        entry_price=candidate.entry_price,
        stop_price=candidate.stop_price,
    )
    risk=VR.with_proposed_position(current_positions,proposed,correlations)
    spec=instrument_registry.readiness(candidate.asset)
    promotion=VPROM.promotion_gate(promotion_evidence)
    model_match=(str(promotion_evidence.model_version)==str(candidate.model_version))
    promoted=bool(promotion.get("eligible_for_production") and model_match)

    gate=VX.production_order_gate(
        candidate.asset,
        candidate.plan,
        source_gate=candidate.source_gate,
        durable_storage=durable_storage,
        calibrated_probability=candidate.calibrated_probability,
        stop_risk_nav=risk.get("proposed_stop_risk_nav"),
        single_asset_fraction=abs(float(candidate.fraction_nav)),
        gross_after=candidate.gross_after,
        drawdown=candidate.drawdown,
        total_open_stop_risk_nav_after=risk.get("total_open_stop_risk_nav"),
        correlated_stop_risk_nav_after=risk.get("max_correlated_stop_risk_nav"),
        instrument_spec_validated=bool(spec.get("ready")),
        model_promoted=promoted,
        model_version=candidate.model_version,
        daily_pnl_pct=candidate.daily_pnl_pct,
        weekly_pnl_pct=candidate.weekly_pnl_pct,
        broker_reconciled=broker_reconciled,
        kill_switch=kill_switch,
        risk_profile=risk_profile,
        probability_policy=probability_policy,
    )

    blockers=list(gate.get("blockers") or [])
    blockers.extend(risk.get("blockers") or [])
    if not model_match:
        blockers.append("MODEL_VERSION_MISMATCH")
    if not spec.get("ready"):
        blockers.extend(spec.get("blockers") or ["INSTRUMENT_SPEC_NOT_READY"])
    arm=_armed()
    if not arm["enabled"]:
        blockers.append("LIVE_EXECUTION_DISABLED")
    if not arm["armed"]:
        blockers.append("LIVE_EXECUTION_NOT_ARMED")

    blockers=list(dict.fromkeys(blockers))
    ok=bool(gate.get("eligible")) and not blockers
    return {
        "eligible":ok,
        "status":"PASS" if ok else "BLOCK",
        "blockers":blockers,
        "candidate":{
            "asset":candidate.asset,"direction":candidate.direction,
            "fraction_nav":candidate.fraction_nav,"model_version":candidate.model_version,
        },
        "order_gate":gate,
        "risk":risk,
        "instrument":spec,
        "promotion":promotion,
        "risk_profile":dict(risk_profile or {}),
        "probability_policy":dict(probability_policy or {}),
        "model_version_match":model_match,
        "live_switch":arm,
        "version":VERSION,
        "principle":"A live order exists only if every independent safety domain passes simultaneously.",
    }
