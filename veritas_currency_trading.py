"""Durable Currency proposals and approved-order orchestration.

The constructor is inert. Execution is disabled unless an explicit bool enables
this coordinator AND the broker adapter's independent gates permit submission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import uuid

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
from veritas_currency_trade_plan import (
    AccountSnapshot, BrokerQuote, ContractSpec, TradePlanBlocked, currency_limits,
    decimal, fingerprint, integer, json_safe, prepare_entry, prepare_exit,
    revalidate, select_entry, utc,
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
                 clock=None):
        if type(execution_enabled) is not bool:
            raise TradePlanBlocked("BOOLEAN_EXECUTION_GATE_REQUIRED")
        if not isinstance(account_id, str) or not account_id.strip():
            raise TradePlanBlocked("EXPLICIT_ACCOUNT_REQUIRED")
        if not isinstance(owner, TradeOwner):
            raise TradePlanBlocked("EXPLICIT_TRADE_OWNER_REQUIRED")
        if type(approval_ttl_seconds) is not int or not 15 <= approval_ttl_seconds <= 300:
            raise TradePlanBlocked("INVALID_APPROVAL_TTL")
        self.repository, self.adapter = repository, adapter
        self.account_id, self.owner = account_id, owner
        self.facts, self.summary, self.ingest_execution = facts, summary, ingest_execution
        self.execution_enabled = execution_enabled
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
            row, admission = select_entry(self.summary(), facts.account, now)
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
        terms["protective_order_mode"] = "EXIT_REQUIRES_SEPARATE_CONFIRMATION"
        terms["execution_environment"] = self.adapter.environment
        return self.repository.create(terms, owner_user_id=self.owner.user_id,
            private_chat_id=self.owner.private_chat_id, bot_id=self.owner.bot_id,
            expires_at=now + timedelta(seconds=self.approval_ttl_seconds),
            economics_revision=1)

    def prepare(self, requested_exit=None):
        with self._lock:
            now = utc(self.clock())
            return self._create(self._checked_facts(), now, requested_exit)

    def prepare_next(self):
        with self._lock:
            now, facts = utc(self.clock()), self._checked_facts()
            held = facts.held_terms or {}
            reason = None
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
                    row = VCR.currency_candidate_book(self.summary()).get("CNYRUBF")
                    if row:
                        signal = VCR.evaluate(row, CTC.runtime_portfolio_policy("Currency"), 0.0, now)
                        direction = (signal.get("prepared_plan") or {}).get("direction")
                        if signal.get("open") is True and direction == ("SHORT" if sign > 0 else "LONG"):
                            reason = "CONFIRMED_OPPOSITE_CANONICAL_EVENT"
                if reason:
                    event_id = "EXIT-" + fingerprint({
                        "entry": held.get("canonical_event_id"),
                        "ledger_revision": facts.account.ledger_revision, "reason": reason})
                    return self._create(facts, now, {"event_id": event_id, "reason": reason})
            return self._create(facts, now)

    def _event_still_valid(self, terms, facts, now):
        if terms.get("action") in ("REDUCE", "CLOSE"):
            return True
        if terms.get("policy_version") != CTC.VERSION:
            return False
        try:
            row, admission = select_entry(self.summary(), facts.account, now)
            current = prepare_entry(row, admission, facts.spec, facts.account,
                                    facts.quote, now=now, held_terms=facts.held_terms)
            fixed = ("canonical_event_id", "direction", "horizon", "source_identity",
                     "stop_price", "target_price", "action")
            if any(current.get(k) != terms.get(k) for k in fixed):
                return False
            old_event = (terms.get("entry_context") or {}).get("event")
            new_event = (current.get("entry_context") or {}).get("event")
            return fingerprint(old_event) == fingerprint(new_event) and integer(terms["lots"]) <= integer(current["lots"])
        except (TradePlanBlocked, KeyError, TypeError, ValueError):
            return False

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
            try:
                now, facts = utc(self.clock()), self._checked_facts()
                revalidate(terms, facts.spec, facts.account, facts.quote, now=now,
                           canonical_event_valid=self._event_still_valid(terms, facts, now))
            except TradePlanBlocked as exc:
                self.repository.block(proposal_id, str(exc))
                return {"ok": False, "code": str(exc)}
            claimed = self.repository.claim_approved(proposal_id, terms_hash=proposal["terms_hash"],
                worker_id=self.worker_id, economics_revision=proposal.get("economics_revision", 1))
            if not claimed:
                return {"ok": False, "code": "APPROVAL_ALREADY_CLAIMED_OR_EXPIRED"}
            try:
                result = self.adapter.submit_limit(self.account_id, terms["instrument_uid"],
                    terms["side"], integer(terms["lots"]), decimal(terms["limit_price"]),
                    claimed["client_order_id"], time_in_force=terms["time_in_force"])
            except Exception:
                self.repository.record_submission(proposal_id, claimed["claim_token"],
                    outcome="UNKNOWN", filled_lots=None, client_order_id=claimed["client_order_id"])
                return {"ok": False, "code": "BROKER_RESULT_UNKNOWN"}
            status = result.status
            outcome = result.outcome if result.outcome in ("ACCEPTED", "REJECTED", "UNKNOWN") else "UNKNOWN"
            if outcome == "ACCEPTED" and result.lots_executed is None:
                outcome, status = "UNKNOWN", "UNKNOWN"
            self.repository.record_submission(proposal_id, claimed["claim_token"],
                outcome=outcome, broker_order_id=result.broker_order_id,
                broker_status=status, filled_lots=result.lots_executed if outcome != "UNKNOWN" else None,
                client_order_id=claimed["client_order_id"], observed_at=result.observed_at,
                average_fill_price=result.average_fill_price)
            if outcome == "REJECTED" and not result.broker_order_id and result.lots_executed in (None, 0):
                self.repository.mark_execution_reconciled(proposal_id, None, 0)
            return {"ok": outcome == "ACCEPTED", "code": status, "proposal_id": proposal_id}

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
                            or (result.client_order_id and result.client_order_id != proposal["client_order_id"])
                            or result.instrument_uid != terms.get("instrument_uid")
                            or result.side != terms.get("side")
                            or result.lots_requested != integer(terms["lots"])):
                        raise TradePlanBlocked("BROKER_ORDER_IDENTITY_MISMATCH")
                    self.ingest_execution(proposal, result)
                    self.repository.update_execution(proposal_id, broker_order_id=result.broker_order_id,
                        broker_status=result.status, filled_lots=result.lots_executed,
                        client_order_id=proposal["client_order_id"], observed_at=result.observed_at,
                        average_fill_price=result.average_fill_price)
                    if result.status in ("FILLED", "CANCELLED", "REJECTED", "BROKER_REJECTED"):
                        self.repository.mark_execution_reconciled(proposal_id, result.broker_order_id,
                                                                   result.lots_executed)
                    results.append({"proposal_id": proposal_id, "code": result.status})
                except Exception:
                    results.append({"proposal_id": proposal_id, "code": "EXECUTION_RECONCILIATION_PENDING"})
        return results
