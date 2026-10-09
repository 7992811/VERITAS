"""Durable Currency proposals and approved-order orchestration.

The constructor is inert. Execution is disabled unless an explicit bool enables
this coordinator AND the broker adapter's independent gates permit submission.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import threading
import re
import uuid

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_timeframe_policy as TFP
from veritas_tbank_trading import PreSubmissionBlocked
from veritas_trade_approvals import ApprovalError, canonical_terms
from veritas_currency_trade_plan import (
    AccountSnapshot, BrokerQuote, ContractSpec, TradePlanBlocked, currency_limits,
    _broker_context, approved_entry_context, decimal, fingerprint, fresh, integer,
    json_safe, prepare_entry, prepare_exit, revalidate, select_entry, utc,
)


@dataclass(frozen=True)
class TradeOwner:
    user_id: int
    private_chat_id: int
    bot_id: int

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in
               (self.user_id, self.private_chat_id, self.bot_id)):
            raise TradePlanBlocked("EXPLICIT_TRADE_OWNER_REQUIRED")
        if self.user_id != self.private_chat_id:
            raise TradePlanBlocked("OWNER_PRIVATE_CHAT_REQUIRED")


@dataclass(frozen=True)
class TradeFacts:
    spec: ContractSpec
    account: AccountSnapshot
    quote: BrokerQuote
    held_terms: dict | None = None


class CurrencyTradingCoordinator:
    def __init__(self, *, repository, adapter, account_id, owner, facts, summary,
                 ingest_execution, execution_enabled=False, approval_ttl_seconds=120,
                 clock=None, live_admission=None, preflight_live=False, manual_admission=None,
                 sandbox_autotrade_enabled=False):
        if type(execution_enabled) is not bool:
            raise TradePlanBlocked("BOOLEAN_EXECUTION_GATE_REQUIRED")
        if not isinstance(account_id, str) or not account_id.strip():
            raise TradePlanBlocked("EXPLICIT_ACCOUNT_REQUIRED")
        if not isinstance(owner, TradeOwner):
            raise TradePlanBlocked("EXPLICIT_TRADE_OWNER_REQUIRED")
        if type(approval_ttl_seconds) is not int or not 15 <= approval_ttl_seconds <= 300:
            raise TradePlanBlocked("INVALID_APPROVAL_TTL")
        if live_admission is not None and not callable(live_admission):
            raise TradePlanBlocked("INVALID_LIVE_ADMISSION_CHECKER")
        if type(preflight_live) is not bool:
            raise TradePlanBlocked("BOOLEAN_PREFLIGHT_GATE_REQUIRED")
        if type(sandbox_autotrade_enabled) is not bool:
            raise TradePlanBlocked("BOOLEAN_SANDBOX_AUTOTRADE_GATE_REQUIRED")
        if sandbox_autotrade_enabled and getattr(adapter, "environment", None) != "sandbox":
            raise TradePlanBlocked("SANDBOX_AUTOTRADE_PRODUCTION_FORBIDDEN")
        self.live_admission = live_admission
        if manual_admission is not None and not callable(manual_admission):
            raise TradePlanBlocked("INVALID_MANUAL_ADMISSION_CHECKER")
        self.manual_admission = manual_admission
        self.preflight_live = preflight_live
        self.repository, self.adapter = repository, adapter
        self.account_id, self.owner = account_id, owner
        self.facts, self.summary, self.ingest_execution = facts, summary, ingest_execution
        self.execution_enabled = execution_enabled
        self.sandbox_autotrade_enabled = sandbox_autotrade_enabled
        self.approval_ttl_seconds = approval_ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.worker_id = "currency-" + uuid.uuid4().hex

    def _checked_facts(self):
        facts = self.facts()
        if not isinstance(facts, TradeFacts) or facts.account.account_id != self.account_id:
            raise TradePlanBlocked("BOUND_ACCOUNT_MISMATCH")
        if facts.spec.instrument_uid != facts.quote.instrument_uid:
            raise TradePlanBlocked("BOUND_INSTRUMENT_MISMATCH")
        return facts

    def _owner_matches(self, proposal):
        return (proposal.get("owner_user_id"), proposal.get("private_chat_id"),
                proposal.get("bot_id")) == (self.owner.user_id,
                self.owner.private_chat_id, self.owner.bot_id)

    def _create(self, facts, now, requested_exit=None):
        if requested_exit is None:
            row, admission = select_entry(self.summary(), facts.account, now, spec=facts.spec, quote=facts.quote)
            terms = prepare_entry(row, admission, facts.spec, facts.account,
                                  facts.quote, now=now, held_terms=facts.held_terms)
        else:
            request = dict(requested_exit)
            held = facts.held_terms or {}
            terms = prepare_exit(facts.spec, facts.account, facts.quote, now=now,
                event_id=request["event_id"], reason=request["reason"],
                lots=request.get("lots"), horizon=request.get("horizon") or held.get("horizon"),
                stop_price=held.get("stop_price"), target_price=held.get("target_price"),
                source_identity=request.get("source_identity") or held.get("source_identity"))
        if requested_exit is not None:
            terms["exit_trigger_event_id"] = requested_exit.get("trigger_event_id")
            terms["exit_trigger_context"] = requested_exit.get("trigger_context")
            terms["held_entry_event_id"] = (facts.held_terms or {}).get("canonical_event_id")
            if (facts.held_terms or {}).get("decision_authority"):
                terms["position_decision_authority"] = facts.held_terms["decision_authority"]
        terms["protective_order_mode"] = "EXIT_REQUIRES_SEPARATE_CONFIRMATION"
        terms["execution_environment"] = self.adapter.environment
        # The authority and durable proposal must bind the same immutable bytes,
        # including canonical decimal strings used by the approval repository.
        terms = canonical_terms(terms)
        if self.preflight_live and self._require_live_admission(terms, facts, now):
            now = utc(self.clock())
            revalidate(terms, facts.spec, facts.account, facts.quote, now=now,
                       canonical_event_valid=self._event_still_valid(terms, facts, now))
        create = self.repository.create_exit_successor if requested_exit is not None else self.repository.create
        return create(terms, owner_user_id=self.owner.user_id,
            private_chat_id=self.owner.private_chat_id, bot_id=self.owner.bot_id,
            expires_at=now + timedelta(seconds=self.approval_ttl_seconds),
            economics_revision=1)

    def prepare(self, requested_exit=None):
        with self._lock:
            facts = self._checked_facts()
            return self._create(facts, utc(self.clock()), requested_exit)

    def prepare_manual(self, request, *, reviewed_terms=None):
        """Prepare only an explicit owner's intent; no summary/signal is read."""
        import veritas_currency_manual as M
        with self._lock:
            intent = M.request(request)
            existing = self.repository.get_by_event(self.account_id, M.event_id(intent), intent["action"])
            if existing is None and intent['action'] == 'OPEN':
                existing = self.repository.get_by_event(self.account_id, M.event_id(intent), 'ADD')
            if existing:
                if (not self._owner_matches(existing)
                        or existing["terms"].get("manual_request_hash") != fingerprint(intent)
                        or existing["terms"].get("execution_environment") != self.adapter.environment):
                    raise TradePlanBlocked("MANUAL_REQUEST_ID_REUSED")
                return existing
            facts, now = self._checked_facts(), utc(self.clock())
            if reviewed_terms is None:
                terms = M.prepare(intent, facts, now, ttl_seconds=self.approval_ttl_seconds)
                terms.update(execution_environment=self.adapter.environment,
                    manual_owner_user_id=self.owner.user_id, manual_private_chat_id=self.owner.private_chat_id,
                    manual_bot_id=self.owner.bot_id)
                terms = canonical_terms(terms)
            else:
                terms = canonical_terms(reviewed_terms)
                if (terms.get("manual_request") != intent
                        or terms.get("manual_owner_user_id") != self.owner.user_id
                        or terms.get("manual_private_chat_id") != self.owner.private_chat_id
                        or terms.get("manual_bot_id") != self.owner.bot_id
                        or terms.get("execution_environment") != self.adapter.environment):
                    raise TradePlanBlocked("MANUAL_APPROVED_TERMS_CHANGED")
                M.validate_intent(terms, facts.spec, facts.account, facts.quote, now)
                revalidate(terms, facts.spec, facts.account, facts.quote, now=now)
            verdict = self._require_live_admission(terms, facts, now)
            now = utc(self.clock())
            revalidate(terms, facts.spec, facts.account, facts.quote, now=now)
            if verdict and verdict.get("valid_until") and utc(verdict["valid_until"]) <= now:
                raise TradePlanBlocked("LIVE_ADMISSION_EXPIRED")
            return self.repository.create(terms, owner_user_id=self.owner.user_id,
                private_chat_id=self.owner.private_chat_id, bot_id=self.owner.bot_id,
                expires_at=utc(terms["manual_intent_expires_at"]), economics_revision=1)

    def _validate_reviewed_entry(self, terms, facts, now):
        """Admit only an unchanged canonical order, using new observed facts.

        Quote/account timestamps can advance while independently signed evidence
        is prepared. Any economic, sizing, provenance or execution-term change
        needs another review, even when it would be favorable to the account.
        """
        if (self.adapter.environment != "production"
                or terms.get("execution_environment") != "production"
                or terms.get("action") not in ("OPEN", "ADD")):
            raise TradePlanBlocked("REVIEWED_PRODUCTION_ENTRY_REQUIRED")
        row, admission = select_entry(self.summary(), facts.account, now, spec=facts.spec, quote=facts.quote)
        current = prepare_entry(row, admission, facts.spec, facts.account, facts.quote,
                                now=now, held_terms=facts.held_terms)
        current.update(protective_order_mode="EXIT_REQUIRES_SEPARATE_CONFIRMATION",
                       execution_environment="production")
        current = canonical_terms(current)
        changing = {"prepared_at", "quote_observed_at", "account_observed_at",
                    "entry_context", "entry_context_json"}
        if (terms.keys() != current.keys()
                or fingerprint({k: v for k, v in terms.items() if k not in changing})
                != fingerprint({k: v for k, v in current.items() if k not in changing})):
            raise TradePlanBlocked("REVIEWED_CANONICAL_TERMS_CHANGED")
        prepared = utc(terms["prepared_at"])
        if (prepared > now or utc(terms["quote_observed_at"]) > utc(facts.quote.observed_at)
                or utc(terms["account_observed_at"]) > utc(facts.account.observed_at)):
            raise TradePlanBlocked("REVIEWED_OBSERVATION_FROM_FUTURE")
        fresh(terms["quote_observed_at"], prepared, 15, "REVIEWED_ORIGINAL_QUOTE_STALE")
        fresh(terms["account_observed_at"], prepared, 30, "REVIEWED_ORIGINAL_ACCOUNT_STALE")
        native = approved_entry_context(terms)
        old_quote = replace(facts.quote, observed_at=utc(terms["quote_observed_at"]))
        original = _broker_context(native, facts.spec, old_quote, terms["direction"], prepared)
        rebound = _broker_context(native, facts.spec, facts.quote, terms["direction"], now)
        if (fingerprint(original) != fingerprint(native)
                or fingerprint(rebound) != fingerprint(approved_entry_context(current))):
            raise TradePlanBlocked("REVIEWED_CANONICAL_CONTEXT_CHANGED")
        revalidate(terms, facts.spec, facts.account, facts.quote, now=now,
                   canonical_event_valid=True)

    def prepare_reviewed_entry(self, terms):
        """Retry an immutable LIVE preview; still requires private owner approval."""
        with self._lock:
            try:
                reviewed = canonical_terms(terms)
                if type(terms) is not dict or fingerprint(reviewed) != fingerprint(terms):
                    raise TradePlanBlocked("REVIEWED_CANONICAL_TERMS_REQUIRED")
            except ApprovalError:
                raise TradePlanBlocked("REVIEWED_TERMS_INVALID") from None
            facts, now = self._checked_facts(), utc(self.clock())
            self._validate_reviewed_entry(reviewed, facts, now)
            # This path always requires the real production authority, including
            # when ordinary proposal preflight was not enabled by configuration.
            verdict = self._require_live_admission(reviewed, facts, now)
            if not verdict:
                raise TradePlanBlocked("LIVE_ACCOUNT_ADMISSION_REQUIRED")
            facts, now = self._checked_facts(), utc(self.clock())
            self._validate_reviewed_entry(reviewed, facts, now)
            now = utc(self.clock())
            if verdict.get("valid_until") is not None and utc(verdict["valid_until"]) <= now:
                raise TradePlanBlocked("LIVE_ADMISSION_EXPIRED")
            return self.repository.create(reviewed, owner_user_id=self.owner.user_id,
                private_chat_id=self.owner.private_chat_id, bot_id=self.owner.bot_id,
                expires_at=now + timedelta(seconds=self.approval_ttl_seconds),
                economics_revision=1)

    def prepare_next(self):
        with self._lock:
            facts = self._checked_facts()
            now = utc(self.clock())
            held = facts.held_terms or {}
            reason, trigger_event_id, trigger_context = None, None, None
            if facts.account.managed_signed_lots:
                sign = 1 if facts.account.managed_signed_lots > 0 else -1
                price = decimal(facts.quote.bid if sign > 0 else facts.quote.ask)
                stop, target = held.get("stop_price"), held.get("target_price")
                if facts.account.drawdown >= currency_limits()["hard_drawdown"]:
                    reason = "CURRENCY_DRAWDOWN_LIMIT"
                elif stop is not None and sign * (price - decimal(stop)) <= 0:
                    reason = "STRUCTURAL_STOP_REACHED"
                elif target is not None and sign * (price - decimal(target)) >= 0:
                    reason = "STRATEGY_TARGET_REACHED"
                else:
                    opposite = "SHORT" if sign > 0 else "LONG"
                    eligible = []
                    for row in self.summary():
                        context = row.get("timeframe_entry_context") or {}
                        event = context.get("event") or {}
                        if (row.get("asset") != "CNYRUBF" or row.get("horizon") != held.get("horizon")
                                or json_safe(context.get("source_identity")) != held.get("source_identity")
                                or event.get("direction") != opposite or not event.get("event_id")):
                            continue
                        gate = TFP.entry_gate(row, float(price), opposite, now)
                        confirmation = VCR.local_confirmation_gate(row, gate)
                        if gate.get("eligible") is True and confirmation.get("eligible") is True:
                            eligible.append((str(event.get("event_id")), context, gate))
                    if eligible:
                        event_id, trigger_context, gate = max(eligible,
                            key=lambda item: (TFP.TS.timestamp(item[1]["event"].get("signal_at")) or 0, item[0]))
                        reason, trigger_event_id = "CONFIRMED_OPPOSITE_CANONICAL_EVENT", event_id
                if reason:
                    event_id = "EXIT-" + fingerprint({
                        "entry": held.get("canonical_event_id"),
                        "ledger_revision": facts.account.ledger_revision, "reason": reason,
                        "trigger_event_id": trigger_event_id})
                    return self._create(facts, now, {"event_id": event_id, "reason": reason,
                                                    "trigger_event_id": trigger_event_id,
                                                    "trigger_context": json_safe(trigger_context)})
            return self._create(facts, now)

    def _event_still_valid(self, terms, facts, now):
        if terms.get("action") in ("REDUCE", "CLOSE"):
            return True
        import veritas_currency_manual as M
        if M.applies(terms):
            try:
                M.validate_intent(terms, facts.spec, facts.account, facts.quote, now)
                return True
            except (TradePlanBlocked, TypeError, ValueError):
                return False
        if terms.get("policy_version") != CTC.VERSION:
            return False
        try:
            row, admission = select_entry(self.summary(), facts.account, now, spec=facts.spec, quote=facts.quote)
            current = canonical_terms(prepare_entry(row, admission, facts.spec, facts.account,
                                    facts.quote, now=now, held_terms=facts.held_terms))
            fixed = ("canonical_event_id", "direction", "horizon", "source_identity",
                     "stop_price", "target_price", "action", "plan_version")
            if any(current.get(k) != terms.get(k) for k in fixed):
                return False
            old_event = (terms.get("entry_context") or {}).get("event")
            new_event = (current.get("entry_context") or {}).get("event")
            return fingerprint(old_event) == fingerprint(new_event) and integer(terms["lots"]) <= integer(current["lots"])
        except (ApprovalError, TradePlanBlocked, KeyError, TypeError, ValueError):
            return False

    def _require_live_admission(self, terms, facts, now):
        if self.adapter.environment != "production" or terms.get("action") not in ("OPEN", "ADD"):
            return False
        import veritas_currency_manual as M
        manual = M.applies(terms)
        authority = self.manual_admission if manual else self.live_admission
        if authority is None:
            raise TradePlanBlocked("LIVE_ACCOUNT_ADMISSION_REQUIRED")
        try:
            verdict = authority(terms=deepcopy(terms), facts=facts, now=now)
            admitted = (isinstance(verdict, Mapping) and verdict.get("eligible") is True
                        and type(verdict.get("blockers")) in (list, tuple)
                        and len(verdict["blockers"]) == 0)
        except Exception:
            raise TradePlanBlocked("LIVE_ACCOUNT_ADMISSION_REQUIRED") from None
        if not admitted:
            if manual and isinstance(verdict, Mapping):
                codes = verdict.get("blockers") or []
                if codes and isinstance(codes[0], str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", codes[0]):
                    raise TradePlanBlocked(codes[0])
            raise TradePlanBlocked("LIVE_ACCOUNT_ADMISSION_REQUIRED")
        if verdict.get("valid_until") is not None:
            try:
                if utc(verdict["valid_until"]) <= utc(self.clock()):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise TradePlanBlocked("LIVE_ADMISSION_EXPIRED") from None
        return deepcopy(dict(verdict))

    def execute_approved(self, proposal_id):
        with self._lock:
            proposal = self.repository.get(proposal_id)
            if not proposal or proposal.get("status") != "APPROVED":
                return {"ok": False, "code": "PROPOSAL_NOT_APPROVED"}
            if proposal.get("terms", {}).get("account_id") != self.account_id or not self._owner_matches(proposal):
                return {"ok": False, "code": "APPROVED_OWNER_OR_ACCOUNT_CHANGED"}
            terms = proposal["terms"]
            if terms.get("execution_environment") != self.adapter.environment:
                self.repository.block(proposal_id, "EXECUTION_ENVIRONMENT_CHANGED")
                return {"ok": False, "code": "EXECUTION_ENVIRONMENT_CHANGED"}
            if self.execution_enabled is not True:
                return {"ok": False, "code": "BROKER_EXECUTION_DISABLED"}
            live_verdict = False
            try:
                facts = self._checked_facts()
                now = utc(self.clock())
                revalidate(terms, facts.spec, facts.account, facts.quote, now=now,
                           canonical_event_valid=self._event_still_valid(terms, facts, now))
                live_verdict = self._require_live_admission(terms, facts, now)
                if live_verdict:
                    # The whole-account authority may perform I/O. Its duration
                    # cannot extend the approved quote or native event lifetime.
                    now = utc(self.clock())
                    revalidate(terms, facts.spec, facts.account, facts.quote, now=now,
                               canonical_event_valid=self._event_still_valid(terms, facts, now))
            except TradePlanBlocked as exc:
                self.repository.block(proposal_id, str(exc))
                return {"ok": False, "code": str(exc)}
            claimed, submission_started, response = None, False, None
            guard = getattr(self, "execution_guard", None)
            if guard is not None and not callable(guard):
                self.repository.block(proposal_id, "INVALID_EXECUTION_GUARD")
                return {"ok": False, "code": "INVALID_EXECUTION_GUARD"}

            def check_before_send(check_permission=True):
                permission = getattr(self, "execution_permission", None)
                if check_permission and callable(permission):
                    try:
                        permitted = permission() is True
                    except Exception:
                        permitted = False
                    if not permitted:
                        raise TradePlanBlocked("EXECUTION_PAUSED")
                checked_at = utc(self.clock())
                if utc(proposal["expires_at"]) <= checked_at:
                    raise TradePlanBlocked("APPROVAL_EXPIRED_BEFORE_SUBMISSION")
                revalidate(terms, facts.spec, facts.account, facts.quote, now=checked_at,
                           canonical_event_valid=self._event_still_valid(terms, facts, checked_at))
                if live_verdict and live_verdict.get("valid_until") is not None:
                    if utc(live_verdict["valid_until"]) <= checked_at:
                        raise TradePlanBlocked("LIVE_ADMISSION_EXPIRED")
                return True

            try:
                # The production console guard holds the same durable config
                # row lock as pause/disable until the one attempt is recorded.
                # An earlier in-flight send must finish before pause can return.
                with (guard() if callable(guard) else nullcontext(True)) as permitted:
                    if permitted is not True:
                        raise TradePlanBlocked("EXECUTION_PAUSED")
                    check_before_send()
                    claimed = self.repository.claim_approved(
                        proposal_id, terms_hash=proposal["terms_hash"],
                        worker_id=self.worker_id, economics_revision=proposal.get("economics_revision", 1),
                        allow_sandbox_auto=self.sandbox_autotrade_enabled)
                    if not claimed:
                        return {"ok": False, "code": "APPROVAL_ALREADY_CLAIMED_OR_EXPIRED"}
                    # A database wait cannot refresh the quote, native event or
                    # independently audited LIVE evidence deadline.
                    check_before_send()
                    submission_started = True
                    try:
                        result = self.adapter.submit_limit(self.account_id, terms["instrument_uid"],
                            terms["side"], integer(terms["lots"]), decimal(terms["limit_price"]),
                            claimed["client_order_id"], time_in_force=terms["time_in_force"],
                            pre_send_check=lambda: check_before_send(check_permission=not callable(guard)))
                    except PreSubmissionBlocked as exc:
                        # Only this adapter-owned type proves no mutation was
                        # attempted, even though read-side preflight has run.
                        submission_started = False
                        raise TradePlanBlocked(exc.code) from None
                    except Exception:
                        self.repository.record_submission(proposal_id, claimed["claim_token"],
                            outcome="UNKNOWN", filled_lots=None, client_order_id=claimed["client_order_id"])
                        response = {"ok": False, "code": "BROKER_RESULT_UNKNOWN", "proposal_id": proposal_id}
                    else:
                        status = result.status
                        outcome = result.outcome if result.outcome in ("ACCEPTED", "REJECTED", "UNKNOWN") else "UNKNOWN"
                        identity_mismatch = ((result.client_order_id and result.client_order_id != claimed["client_order_id"])
                            or (result.instrument_uid and result.instrument_uid != terms["instrument_uid"])
                            or (result.side and result.side != terms["side"])
                            or (result.lots_requested is not None and result.lots_requested != integer(terms["lots"])))
                        if identity_mismatch:
                            self.repository.record_submission(proposal_id, claimed["claim_token"],
                                outcome="UNKNOWN", filled_lots=None, client_order_id=claimed["client_order_id"])
                            response = {"ok": False, "code": "BROKER_ORDER_IDENTITY_MISMATCH", "proposal_id": proposal_id}
                        else:
                            if outcome == "ACCEPTED" and result.lots_executed is None:
                                outcome, status = "UNKNOWN", "UNKNOWN"
                            self.repository.record_submission(proposal_id, claimed["claim_token"],
                                outcome=outcome, broker_order_id=result.broker_order_id,
                                broker_status=status, filled_lots=result.lots_executed if outcome != "UNKNOWN" else None,
                                client_order_id=claimed["client_order_id"], observed_at=result.observed_at,
                                average_fill_price=result.average_fill_price)
                            if outcome == "REJECTED" and not result.broker_order_id and result.lots_executed in (None, 0):
                                self.repository.mark_execution_reconciled(proposal_id, None, 0)
                            response = {"ok": outcome == "ACCEPTED", "code": status, "proposal_id": proposal_id}
                if response is None:
                    raise TradePlanBlocked("EXECUTION_GUARD_SUPPRESSED_REFUSAL")
                return response
            except Exception as exc:
                if submission_started:
                    # The adapter may have reached the broker. Never abort or
                    # requeue this UUID, including on guard-release/DB failure.
                    if response is not None:
                        return dict(response, guard_release_confirmed=False)
                    return {"ok": False, "code": "EXECUTION_RECONCILIATION_PENDING", "proposal_id": proposal_id}
                code = getattr(exc, "code", str(exc) if isinstance(exc, TradePlanBlocked) else None)
                if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
                    code = "EXECUTION_GUARD_UNAVAILABLE"
                try:
                    if claimed is not None:
                        self.repository.abort_unsubmitted_claim(proposal_id, claimed["claim_token"], code)
                    else:
                        self.repository.block(proposal_id, code)
                except Exception:
                    return {"ok": False, "code": "EXECUTION_RECONCILIATION_PENDING", "proposal_id": proposal_id}
                return {"ok": False, "code": code, "proposal_id": proposal_id}

    def reconcile(self):
        results = []
        with self._lock:
            pending = self.repository.list_unsettled(account_id=self.account_id, limit=100)
            for proposal in pending:
                terms = proposal.get("terms") or {}
                proposal_id = proposal["proposal_id"]
                if terms.get("account_id") != self.account_id:
                    continue
                if terms.get("execution_environment") != self.adapter.environment:
                    results.append({"proposal_id": proposal_id, "code": "EXECUTION_ENVIRONMENT_CHANGED"})
                    continue
                try:
                    if proposal.get("broker_order_id"):
                        result = self.adapter.get_order(self.account_id, proposal["broker_order_id"])
                    else:
                        result = self.adapter.get_order(self.account_id, client_order_id=proposal["client_order_id"])
                    if result.status == "UNKNOWN" or result.lots_executed is None:
                        results.append({"proposal_id": proposal_id, "code": "BROKER_RESULT_UNKNOWN"})
                        continue
                    if (not result.broker_order_id
                            or (proposal.get("broker_order_id") and result.broker_order_id != proposal["broker_order_id"])
                            or (not proposal.get("broker_order_id") and result.client_order_id != proposal["client_order_id"])
                            or (result.client_order_id and result.client_order_id != proposal["client_order_id"])
                            or result.instrument_uid != terms.get("instrument_uid")
                            or result.side != terms.get("side")
                            or result.lots_requested != integer(terms["lots"])):
                        raise TradePlanBlocked("BROKER_ORDER_IDENTITY_MISMATCH")
                    self.ingest_execution(proposal, result)
                    self.repository.update_execution(proposal_id, broker_order_id=result.broker_order_id,
                        broker_status=result.status, filled_lots=result.lots_executed,
                        client_order_id=result.client_order_id, observed_at=result.observed_at,
                        average_fill_price=result.average_fill_price)
                    if result.status in ("FILLED", "CANCELLED", "REJECTED", "BROKER_REJECTED"):
                        self.repository.mark_execution_reconciled(proposal_id, result.broker_order_id,
                                                                   result.lots_executed)
                    results.append({"proposal_id": proposal_id, "code": result.status})
                except Exception:
                    results.append({"proposal_id": proposal_id, "code": "EXECUTION_RECONCILIATION_PENDING"})
        return results
