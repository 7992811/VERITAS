"""Whole-account controls for owner-directed trials; no model promotion claim."""
from copy import deepcopy
from datetime import timedelta

import veritas_currency_live_admission as L
import veritas_currency_manual as M
import veritas_currency_trade_plan as P
import veritas_execution as VX
import veritas_live as LIVE


def binding(terms):
    """Explicit manual identity inside the existing signed evidence envelope."""
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
    """Initial scope: one contract from a flat whole account, same live risk caps."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last.update(decision_authority=M.VERSION,
            blockers=["LIVE_ACCOUNT_HISTORY_EVIDENCE_NOT_CHECKED"],
            required_checks=["OWNER_IMMUTABLE_MANUAL_INTENT", "EXACT_BROKER_CONTRACT_AND_FRESH_QUOTE",
                "FLAT_WHOLE_ACCOUNT", "INDEPENDENT_SIGNED_ACCOUNT_HISTORY",
                "LIVE_ECONOMICS_AND_ACCOUNT_RISK", "LIVE_ENABLED_AND_ARMED"])

    def __call__(self, *, terms, facts, now):
        result = {"eligible": False, "status": "BLOCK", "blockers": [], "version": M.VERSION,
            "decision_authority": M.VERSION, "scope": "WHOLE_BROKER_ACCOUNT",
            "model_admission": "NOT_APPLICABLE_OWNER_DECISION", "evidence_loader_configured": True}
        try:
            started = max(L._date(now), L._date(self.clock()))
            L._require(terms.get("account_id") == self.account_id == facts.account.account_id,
                       "LIVE_CANDIDATE_ACCOUNT_OR_INSTRUMENT_MISMATCH")
            binding(terms)
            P.revalidate(terms, facts.spec, facts.account, facts.quote, now=started)
            scope = L.evidence_scope(terms)
            result["evidence_request"] = {"schema": L.SIGNED_EVIDENCE_SCHEMA, "terms": deepcopy(terms),
                "scope": scope, "scope_hash": L._hash(scope), "evaluated_at": started.isoformat(),
                "required_kinds": ["ACCOUNT_CONTROLS"], "trade_permission": False}
            L._require(self.store.status()["configured"], "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED")
            snapshot, current, _, other_gross, _ = self._snapshot(terms, facts, started)
            L._require(not current and not other_gross and not snapshot["positions"],
                       "MANUAL_TRIAL_REQUIRES_FLAT_WHOLE_ACCOUNT")
            completed = max(started, L._date(self.clock()))
            P.revalidate(terms, facts.spec, facts.account, facts.quote, now=completed)
            L._require(completed-started <= L.MAX_ACCOUNT_AGE, "LIVE_ACCOUNT_SNAPSHOT_STALE")
            result.update(observed_at=started.isoformat(), account_equity_rub=snapshot["equity_rub"],
                account_snapshot_sha256=L._hash(snapshot), broker_positions=0, broker_working_orders=0)
            result["evidence_request"]["account_snapshot_sha256"] = result["account_snapshot_sha256"]
            self.store.record_snapshot(snapshot, started)
            # Authentic account history remains mandatory. No probability,
            # promotion, OOS report or native signal is manufactured for the owner.
            document = self.store.load("ACCOUNT_HISTORY", snapshot["binding"], completed, terms=terms)
            history = L._account_history(document, snapshot, completed)
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
            result["blockers"].extend(VX.production_account_risk_blockers(
                stop_risk_nav=stop_risk, single_asset_fraction=float(fraction), gross_after=float(fraction),
                drawdown=history["drawdown"], total_open_stop_risk_nav_after=stop_risk,
                correlated_stop_risk_nav_after=stop_risk, instrument_spec_validated=True,
                daily_pnl_pct=history["daily_pnl_pct"], weekly_pnl_pct=history["weekly_pnl_pct"],
                broker_reconciled=facts.account.reconciled and facts.account.costs_reconciled,
                kill_switch=history["kill_switch"]))
            arm = LIVE._armed()
            if not arm["enabled"]:
                result["blockers"].append("LIVE_EXECUTION_DISABLED")
            if not arm["armed"]:
                result["blockers"].append("LIVE_EXECUTION_NOT_ARMED")
            result["account_risk"] = dict(history, fraction_nav_after=float(fraction),
                net_stop_risk_nav=stop_risk, net_total_open_stop_risk_nav=stop_risk)
            result["live_authorization"] = {"eligible": not result["blockers"],
                "decision_authority": M.VERSION, "economics": economics, "live_switch": arm}
            result["valid_until"] = min(L._date(document["valid_until"]),
                L._date(document["issuer_valid_until"]), L._date(terms["manual_intent_expires_at"]),
                L._date(facts.quote.observed_at)+timedelta(seconds=15), started+L.MAX_ACCOUNT_AGE).isoformat()
        except (L.AdmissionBlocked, P.TradePlanBlocked) as error:
            result["blockers"].append(getattr(error, "code", str(error)))
        except Exception:
            result["blockers"].append("LIVE_AUTHORITY_DEPENDENCY_UNAVAILABLE")
        return self._finish(result)
