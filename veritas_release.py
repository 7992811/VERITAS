"""Single release identity for VERITAS production, API and audit."""
from __future__ import annotations
import os
import veritas_canonical_constitution as CTC

PRODUCT_VERSION="veritas-max-product-v91.6-signal-readiness"
PORTFOLIO_VERSION="veritas-portfolio-v9.1.6-signal-readiness"
UI_VERSION="veritas-ui-v9.1.6-signal-readiness"
DB_SCHEMA_VERSION="9.0"
PORTFOLIOS=tuple(CTC.PORTFOLIO_ORDER)

def deployment_sha():
    for key in ("RENDER_GIT_COMMIT","RENDER_GIT_COMMIT_SHA","GIT_COMMIT","SOURCE_COMMIT"):
        value=os.getenv(key,"").strip()
        if value:
            return value
    return None

def snapshot():
    sha=deployment_sha()
    return {
        "product_version":PRODUCT_VERSION,
        "portfolio_version":PORTFOLIO_VERSION,
        "ctc_version":CTC.VERSION,
        "strategy_epoch":CTC.STRATEGY_EPOCH,
        "execution_integrity_policy":{
            "version":"CTC_VERIFIED_EXECUTION_SNAPSHOT_V1",
            "one_quote_and_fill_for_admission_and_accounting":True,
            "position_bound_thesis_exit":True,
            "net_stop_risk_sizing":True,
            "incremental_add_funding_from_original_open":True,
            "trailing_requires_post_entry_confirmed_pivot":True,
            "initial_stop_and_target_immutable":True,
        },
        "signal_delivery_policy":{
            "version":"CANONICAL_SIGNAL_READINESS_V1",
            "source_eligibility_separate_from_entry":True,
            "admission_trace_preserves_original_decision":True,
            "closed_bar_boundary_refresh":True,
            "aggregation_preserves_proven_source":True,
        },
        "structural_entry_policy":dict(CTC.STRUCTURAL_ENTRY_POLICY),
        "daily_ma_rebound_policy":dict(CTC.MA_REBOUND_POLICY),
        "active_user_teaching_id":CTC.STRUCTURAL_ENTRY_POLICY["teaching_id"],
        "active_user_teaching_ids":[CTC.STRUCTURAL_ENTRY_POLICY["teaching_id"], CTC.MA_REBOUND_POLICY["teaching_id"]],
        "runtime_authority":CTC.BASIS_RUNTIME,
        "ui_version":UI_VERSION,
        "db_schema_version":DB_SCHEMA_VERSION,
        "portfolio_count":len(PORTFOLIOS),
        "portfolios":list(PORTFOLIOS),
        "deploy_sha":sha,
        "deploy_sha_short":sha[:12] if sha else None,
        "branch":os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH") or None,
    }
