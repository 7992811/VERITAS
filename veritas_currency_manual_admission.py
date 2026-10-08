"""Owner-directed one-contract trials using current whole-account broker facts.

Owner instruction 2026-10-08 removes historical account evidence only for this
manual mode. Historical drawdown/day/week metrics stay unknown, never zero.
Model-generated orders retain their independent evidence requirements.
"""
from copy import deepcopy
from datetime import timedelta

import veritas_currency_live_admission as L
import veritas_currency_manual as M
import veritas_currency_trade_plan as P
import veritas_execution as VX
import veritas_live as LIVE


def binding(terms):
    """Bind immutable manual intent to the current policy and deployment."""
    L._require(M.applies(terms) and terms.get("action") == "OPEN"
        and terms.get("plan_version") == M.PLAN_VERSION and terms.get("model_version") == M.VERSION,
        "MANUAL_PLAN_VERSION_REQUIRED")
    L._require(terms.get("policy_version") == P.CTC.VERSION,
        "PLAN_VERSION_REQUIRES_NEW_APPROVAL")
    intent = M.request(terms.get("manual_request"))
    L._require(terms.get("manual_request_hash") == P.fingerprint(intent)
        and terms.get("canonical_event_id") == M.event_id(intent), "MANUAL_APPROVED_TERMS_CHANGED")
    source = terms.get("source_identity")
    L._require(isinstance(source, dict) and source, "LIVE_SIGNAL_SOURCE_IDENTITY_REQUIRED")
    result = {k: L._text(terms.get(k), "MANUAL_BINDING_REQUIRED") for k in (
        "account_id", "instrument_uid", "asset", "horizon", "direction", "model_version")}
    return dict(result, environment="production", decision_authority=M.VERSION,
        event_family="OWNER_SPECIFIED_LIMIT_ORDER", source_identity_sha256=P.fingerprint(source),
        **L.policy_identity())


class ManualAccountAdmission(L.WholeAccountLiveAdmission):
    """One contract from a flat account, retaining all current risk caps."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last.update(decision_authority=M.VERSION,
            account_history_required=False, historical_risk_controls="NOT_CHECKED_OWNER_MANUAL",
            evidence_loader_configured=False, blockers=["MANUAL_CURRENT_ACCOUNT_NOT_CHECKED"],
            required_checks=["OWNER_IMMUTABLE_MANUAL_INTENT", "EXACT_BROKER_CONTRACT_AND_FRESH_QUOTE",
                "FLAT_WHOLE_ACCOUNT", "DURABLE_CURRENT_ACCOUNT_OBSERVATION",
                "LIVE_ECONOMICS_AND_ACCOUNT_RISK", "LIVE_ENABLED_AND_ARMED"])

    def status(self):
        """Cached manual status never imports history or turns a read into a check."""
        with self._status_lock:
            result = deepcopy(self._last)
        result["admission_cached_only"] = True
        arm = result["live_switch"] = LIVE._armed()
        for field, code in (("enabled", "LIVE_EXECUTION_DISABLED"), ("armed", "LIVE_EXECUTION_NOT_ARMED")):
            if not arm[field]:
                result["blockers"].append(code)
        if result.get("eligible") and L._date(self.clock()) >= L._date(result["valid_until"]):
            result.update(eligible=False, status="STALE")
            result["blockers"].append("LIVE_ADMISSION_RECHECK_REQUIRED")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        if result["blockers"]:
            result["eligible"] = False
            if result["status"] == "PASS":
                result["status"] = "BLOCK"
        return result

    def __call__(self, *, terms, facts, now):
        result = {"eligible": False, "status": "BLOCK", "blockers": [], "version": M.VERSION,
            "decision_authority": M.VERSION, "scope": "WHOLE_BROKER_ACCOUNT",
            "model_admission": "NOT_APPLICABLE_OWNER_DECISION", "evidence_loader_configured": False,
            "account_history_required": False, "historical_risk_controls": "NOT_CHECKED_OWNER_MANUAL",
            "manual_account_risk_policy": "OWNER_CURRENT_ACCOUNT_V1"}
        try:
            started = max(L._date(now), L._date(self.clock()))
            policy = P.CTC.PORTFOLIO_POLICIES["Currency"]
            L._require(policy.get("manual_account_history_required") is False
                and policy.get("manual_account_risk_policy") == result["manual_account_risk_policy"],
                "MANUAL_CURRENT_ACCOUNT_POLICY_REQUIRED")
            L._require(terms.get("account_id") == self.account_id == facts.account.account_id,
                       "LIVE_CANDIDATE_ACCOUNT_OR_INSTRUMENT_MISMATCH")
            binding(terms)
            P.revalidate(terms, facts.spec, facts.account, facts.quote, now=started)
            snapshot, current, _, other_gross, _ = self._snapshot(terms, facts, started)
            L._require(not current and not other_gross and not snapshot["positions"],
                       "MANUAL_TRIAL_REQUIRES_FLAT_WHOLE_ACCOUNT")
            completed = max(started, L._date(self.clock()))
            P.revalidate(terms, facts.spec, facts.account, facts.quote, now=completed)
            L._require(completed-started <= L.MAX_ACCOUNT_AGE, "LIVE_ACCOUNT_SNAPSHOT_STALE")
            result.update(observed_at=started.isoformat(), account_equity_rub=snapshot["equity_rub"],
                account_snapshot_sha256=L._hash(snapshot), broker_positions=0, broker_working_orders=0)
            self.store.record_snapshot(snapshot, started)
            equity = P.decimal(snapshot["equity_rub"], positive=True)
            price, stop, target = (P.decimal(terms[k], positive=True)
                                   for k in ("limit_price", "stop_price", "target_price"))
            exposure = price * facts.spec.rub_per_price_unit_per_lot
            fraction = exposure / equity
            plan = P.live_economics_plan(terms["direction"], price, stop, target, facts.quote, "MANUAL",
                fraction=fraction, expected_hold_seconds=terms["expected_hold_seconds"])
            economics = VX.economics_gate("CNYRUBF", plan, execution_mode="LIVE", now=completed)
            if not economics.get("eligible"):
                result["blockers"].extend(economics.get("blockers") or ["MANUAL_ECONOMICS_BLOCKED"])
            source = VX.production_source_gate("CNYRUBF", {"source_gate_pass": True,
                "market_open": facts.quote.limit_orders_available, "production_direct_feed": True,
                "instrument_uid": facts.spec.instrument_uid,
                "quote_observed_at": facts.quote.observed_at.isoformat()})
            if not source.get("eligible"):
                result["blockers"].append("PRODUCTION_SOURCE_GATE_FAILED")
            stop_risk = float(fraction) * (float(abs(price-stop)/price)
                + float(economics["modeled_round_trip_cost_pct"]))
            result["blockers"].extend(VX.production_current_account_risk_blockers(
                stop_risk_nav=stop_risk, single_asset_fraction=float(fraction), gross_after=float(fraction),
                total_open_stop_risk_nav_after=stop_risk,
                correlated_stop_risk_nav_after=stop_risk, instrument_spec_validated=True))
            if not facts.account.reconciled or not facts.account.costs_reconciled:
                result["blockers"].append("BROKER_RECONCILIATION_REQUIRED")
            arm = LIVE._armed()
            if not arm["enabled"]:
                result["blockers"].append("LIVE_EXECUTION_DISABLED")
            if not arm["armed"]:
                result["blockers"].append("LIVE_EXECUTION_NOT_ARMED")
            result["account_risk"] = dict(drawdown=None, daily_pnl_pct=None, weekly_pnl_pct=None,
                history_status="NOT_CHECKED_OWNER_MANUAL", fraction_nav_after=float(fraction),
                net_stop_risk_nav=stop_risk, net_total_open_stop_risk_nav=stop_risk)
            result["live_authorization"] = {"eligible": not result["blockers"],
                "decision_authority": M.VERSION, "economics": economics, "live_switch": arm}
            result["valid_until"] = min(L._date(terms["manual_intent_expires_at"]),
                L._date(facts.quote.observed_at)+timedelta(seconds=15), started+L.MAX_ACCOUNT_AGE).isoformat()
        except (L.AdmissionBlocked, P.TradePlanBlocked) as error:
            result["blockers"].append(getattr(error, "code", str(error)))
        except Exception:
            result["blockers"].append("LIVE_AUTHORITY_DEPENDENCY_UNAVAILABLE")
        return self._finish(result)
