"""Regime-aware strategy ensemble for VERITAS paper routing.

The ensemble is intentionally a tiebreaker: source/time/risk/economics admission
stays lexicographically ahead of learned weights. Learning therefore cannot
turn an unsafe scenario into an eligible one.
"""
from __future__ import annotations

import veritas_learning_v2 as LV2

VERSION = "REGIME_STRATEGY_ENSEMBLE_V1"


def _scenario(evidence):
    return str((evidence or {}).get("scenario") or "UNKNOWN")[:120]


def rank(base_rank, evidence, raw, horizon):
    """Insert learned weight only after all canonical admission dimensions."""
    base = tuple(base_rank or ())
    asset = str((raw or {}).get("asset") or "")
    regime = str((raw or {}).get("regime") or "UNKNOWN")
    scenario = _scenario(evidence)
    identity = LV2.routing_context(raw)
    overlay = LV2.scenario_weight(asset, str(horizon or ""), regime, scenario,
                                  source_key=identity.get("source_key"),
                                  policy_hash=identity.get("policy_hash"))
    if len(base) >= 5:
        ranked = base[:4] + (float(overlay.get("weight") or 1.0),) + base[4:]
    else:
        ranked = base + (float(overlay.get("weight") or 1.0),)
    blockers = list((evidence or {}).get("economics_blockers") or [])
    shadow = LV2.shadow_advice(asset, str(horizon or ""), regime, scenario, blockers,
                               source_key=identity.get("source_key"),
                               policy_hash=identity.get("policy_hash"))
    audit = {"version": VERSION, "asset": asset, "horizon": horizon, "regime": regime,
             "scenario": scenario, "weight": overlay.get("weight"),
             "source_key": identity.get("source_key"),
             "policy_hash": identity.get("policy_hash"),
             "applied_candidate_ids": overlay.get("candidate_ids") or [],
             "shadow_candidate_ids": shadow.get("candidate_ids") or [],
             "hard_gate_override": False,
             "decision_influence": bool(overlay.get("candidate_ids")),
             "selection_role": "SAFE_TIEBREAKER_ONLY"}
    return ranked, audit
