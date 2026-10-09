"""Single release identity for VERITAS production, API and audit."""
from __future__ import annotations
import os
import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
PRODUCT_VERSION="veritas-max-product-v91.8.27-structural-breakout-learning-v2-registry"
PORTFOLIO_VERSION="veritas-portfolio-v9.1.8.27-structural-breakout-learning-v2-registry"
UI_VERSION="veritas-ui-v9.1.8.27-structural-breakout-learning-v2-registry"
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
            "version":"CTC_VERIFIED_EXECUTION_SNAPSHOT_V2",
            "one_quote_and_fill_for_admission_and_accounting":True,
            "position_bound_thesis_exit":True,
            "net_stop_risk_sizing":True,
            "incremental_add_funding_from_original_open":True,
            "trailing_requires_post_entry_confirmed_pivot":True,
            "initial_stop_and_target_immutable":True,
        },
        "signal_delivery_policy":{
            "version":"CANONICAL_SIGNAL_READINESS_V2",
            "source_eligibility_separate_from_entry":True,
            "admission_trace_preserves_original_decision":True,
            "closed_bar_boundary_refresh":True,
            "aggregation_preserves_proven_source":True,
            "structural_book_contention_nonblocking":True,
            "structural_execution_revalidated_at_wall_clock":True,
            "structural_execution_selects_current_same_source_cached_quote":True,
            "portfolio_positions_and_balances_share_current_book":True,
            "partial_portfolio_response_preserves_visible_positions":True,
            "calibration_reads_projected_decision_fields":True,
            "structural_signal_execution_projection_is_bounded":True,
        },
        "valuation_source_policy":{
            "version":"VALUATION_SOURCE_BASIS_V2", "provider_ticker_checked_when_present":True,
            "brent_source_pin":VPS.brent_feed_pin_identity(), "brent_single_source_required":True,
            "held_brent_contract_from_canonical_identity":True,
            "unqualified_provider_month_not_inferred":True,
        },
        "runtime_read_policy":{
            "version":"BOUNDED_RUNTIME_READS_V14","historical_fields_use_record_projection":True,"legacy_noop_reads_omit_structural_string_owners":True,"book_accounting_io_measured":True,"empty_flip_books_avoid_redundant_reads":True,"owned_legacy_migrations_avoid_rereads":True,"book_payload_compression_checks_server_and_column_policy":True,
            "startup_api_waits_for_bootstrap":True,"single_portfolio_snapshot_refresh":True,
            "snapshot_and_schema_sql_waits_bounded":True,"profit_refresh_positions_projected":True,"post_funding_stop_cost_read_projected":True,"portfolio_http_wait_seconds":1.0,"completed_portfolio_reads_keep_observation_time":True,"protective_fills_advance_snapshot_revision":True,"position_account_payload_reused":True,"entry_decision_display_projected":True,
            "current_portfolio_schema_avoids_ddl":True,"unchanged_portfolio_metadata_avoids_row_locks":True,
            "closed_journal_payload_projected":True,"quality_evidence_root_extracted_once":True,"journal_evidence_root_extracted_once":True,
            "structural_runtime_copies_one_asset":True,"quality_read_timeout_is_transactional":True,
            "completed_prepasses_release_snapshots":True,"quote_caches_project_before_copying":True,"completed_guard_reads_release_positions":True,"quote_prepasses_project_positions":True,"guard_quote_root_extracted_once":True,"guard_no_action_observations_batched":True,"guard_action_reloads_full_locked_position":True,"profit_refresh_writes_batched":True,
            "cost_checks_omit_unused_trade_payload":True,"portfolio_display_payloads_projected_after_accounting":True,"excursion_marks_use_projected_read_and_delta_write":True,"r46_harvest_borrows_current_locked_positions":True,
            "active_cycle_allocator_trims_preserve_caches":True,"archival_snapshot_stages_release_memory":True,"completed_cycle_releases_previous_snapshots":True,"book_cleanup_outside_transaction":True,
            "archival_drift_statistics_stream_all_rows":True,"archival_performance_uses_packed_float_buffers":True,"loss_audit_streams_full_evidence_before_compaction":True,"independent_portfolio_commits":True,"waiting_protection_has_next_turn":True,"aged_ordinary_book_precedes_entry_reservations":True,"protection_precedes_aged_ordinary_book":True,"quality_history_selected_before_projection":True,"quality_review_uses_shared_history_permit":True,
        },
        "structural_entry_policy":dict(CTC.STRUCTURAL_ENTRY_POLICY),
        "breakout_lifecycle_policy":dict(CTC.BREAKOUT_LIFECYCLE_POLICY),
        "daily_ma_rebound_policy":dict(CTC.MA_REBOUND_POLICY),
        "active_user_teaching_id":CTC.BREAKOUT_LIFECYCLE_POLICY["teaching_id"],
        "active_user_teaching_ids":[CTC.STRUCTURAL_ENTRY_POLICY["teaching_id"], CTC.MA_REBOUND_POLICY["teaching_id"], CTC.BREAKOUT_LIFECYCLE_POLICY["teaching_id"]],
        "runtime_authority":CTC.BASIS_RUNTIME,
        "ui_version":UI_VERSION,
        "db_schema_version":DB_SCHEMA_VERSION,
        "portfolio_count":len(PORTFOLIOS),
        "portfolios":list(PORTFOLIOS),
        "deploy_sha":sha,
        "deploy_sha_short":sha[:12] if sha else None,
        "branch":os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH") or None,
    }
