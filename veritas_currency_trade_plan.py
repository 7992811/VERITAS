"""Pure, exact-contract Currency order preparation. No broker I/O or activation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING
from typing import Any, Mapping
import hashlib
import json

import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_execution as VX
import veritas_timeframe_structure as TS

VERSION = "currency-broker-plan-v1"
ASSET = "CNYRUBF"
ZERO, ONE = Decimal("0"), Decimal("1")


class TradePlanBlocked(ValueError):
    """Stable diagnostic code only."""


def decimal(value: Any, *, positive=False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TradePlanBlocked("INVALID_DECIMAL")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TradePlanBlocked("INVALID_DECIMAL") from None
    if not result.is_finite() or (positive and result <= 0):
        raise TradePlanBlocked("INVALID_DECIMAL")
    return result


def integer(value: Any, *, nonnegative=False) -> int:
    number = decimal(value)
    if number != number.to_integral_value() or (nonnegative and number < 0):
        raise TradePlanBlocked("INVALID_LOTS")
    return int(number)


def utc(value: Any) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise TradePlanBlocked("INVALID_TIMESTAMP") from None
    if result.tzinfo is None:
        raise TradePlanBlocked("TIMEZONE_REQUIRED")
    return result.astimezone(timezone.utc)


def fresh(value: Any, now: datetime, age: int, code: str):
    seconds = (utc(now) - utc(value)).total_seconds()
    if seconds < -2 or seconds > age:
        raise TradePlanBlocked(code)


def text_number(value: Decimal) -> str:
    return format(value, "f")


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return text_number(value)
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, float):
        return text_number(decimal(value))
    if isinstance(value, Mapping):
        native_times = {"signal_at", "confirmed_at", "breakout_bar_at",
                        "level_available_at", "stop_level_available_at", "atr_observed_until",
                        "closed_at", "pivot_at", "level_pivot_at", "stop_pivot_at",
                        "available_at", "spent_at"}
        result = {}
        for key, item in value.items():
            if key in native_times and type(item) in (int, float, Decimal):
                try:
                    item = datetime.fromtimestamp(float(decimal(item)), timezone.utc).isoformat()
                except (ValueError, OverflowError, OSError):
                    raise TradePlanBlocked("INVALID_NATIVE_TIMESTAMP") from None
            result[str(key)] = json_safe(item)
        return result
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TradePlanBlocked("UNSUPPORTED_TERMS_VALUE")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(json_safe(value), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ContractSpec:
    instrument_uid: str
    ticker: str
    lot_size: int
    tick_size: Decimal
    tick_value_rub: Decimal
    margin_buy_rub: Decimal
    margin_sell_rub: Decimal
    api_trade_available: bool
    buy_available: bool
    sell_available: bool
    observed_at: datetime

    @property
    def rub_per_price_unit_per_lot(self):
        return decimal(self.tick_value_rub, positive=True) / decimal(self.tick_size, positive=True) * self.lot_size

    def identity(self):
        return json_safe({"instrument_uid": self.instrument_uid, "ticker": self.ticker,
                          "lot_size": self.lot_size, "tick_size": self.tick_size,
                          "tick_value_rub": self.tick_value_rub})

    def validate(self, now):
        if not self.instrument_uid or self.ticker.upper() != ASSET:
            raise TradePlanBlocked("EXACT_CNY_INSTRUMENT_REQUIRED")
        if integer(self.lot_size, nonnegative=True) < 1:
            raise TradePlanBlocked("INVALID_CONTRACT_SPEC")
        decimal(self.tick_size, positive=True)
        decimal(self.tick_value_rub, positive=True)
        fresh(self.observed_at, now, 300, "CONTRACT_SPEC_STALE")
        if not self.api_trade_available:
            raise TradePlanBlocked("INSTRUMENT_API_TRADING_UNAVAILABLE")


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    access_level: str
    currency_nav_rub: Decimal
    high_water_rub: Decimal
    available_margin_rub: Decimal
    signed_lots: int
    managed_signed_lots: int
    blocked_lots: int
    active_order_count: int
    broker_max_buy_lots: int
    broker_max_sell_lots: int
    ledger_revision: int
    observed_at: datetime
    reconciled: bool
    costs_reconciled: bool = True

    @property
    def drawdown(self):
        peak = decimal(self.high_water_rub, positive=True)
        return max(ZERO, ONE - decimal(self.currency_nav_rub) / peak)

    def validate(self, now, *, reducing):
        if not self.account_id:
            raise TradePlanBlocked("EXPLICIT_ACCOUNT_REQUIRED")
        if self.access_level != "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS":
            raise TradePlanBlocked("ACCOUNT_TRADING_ACCESS_REQUIRED")
        fresh(self.observed_at, now, 30, "ACCOUNT_SNAPSHOT_STALE")
        for value in (self.signed_lots, self.managed_signed_lots):
            integer(value)
        for value in (self.blocked_lots, self.active_order_count, self.broker_max_buy_lots,
                      self.broker_max_sell_lots, self.ledger_revision):
            integer(value, nonnegative=True)
        if self.active_order_count or self.blocked_lots:
            raise TradePlanBlocked("WORKING_ORDER_RECONCILIATION_REQUIRED")
        if not reducing and (not self.reconciled or self.signed_lots != self.managed_signed_lots):
            raise TradePlanBlocked("BROKER_POSITION_MISMATCH")
        if not reducing and not self.costs_reconciled:
            raise TradePlanBlocked("BROKER_COST_RECONCILIATION_REQUIRED")


@dataclass(frozen=True)
class BrokerQuote:
    instrument_uid: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    limit_orders_available: bool

    def validate(self, spec, now):
        if self.instrument_uid != spec.instrument_uid:
            raise TradePlanBlocked("QUOTE_INSTRUMENT_MISMATCH")
        bid, ask = decimal(self.bid, positive=True), decimal(self.ask, positive=True)
        if bid > ask:
            raise TradePlanBlocked("CROSSED_ORDER_BOOK")
        if not self.limit_orders_available:
            raise TradePlanBlocked("LIMIT_ORDER_UNAVAILABLE")
        fresh(self.observed_at, now, 15, "BROKER_QUOTE_STALE")


def currency_limits():
    policy = CTC.runtime_portfolio_policy("Currency")
    return {"allocation_rub": decimal(policy["initial_nav_rub"]),
            "max_gross": decimal(policy["max_gross"]),
            "hard_drawdown": decimal(policy["hard_drawdown"])}


def round_price(value, tick, side):
    price = decimal(value, positive=True)
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR if side == "BUY" else ROUND_CEILING) * tick


def select_entry(summary, account, now):
    row = VCR.currency_candidate_book(summary).get(ASSET)
    if not row:
        raise TradePlanBlocked("NO_CURRENCY_CANDIDATE")
    admission = VCR.evaluate(row, CTC.runtime_portfolio_policy("Currency"), float(account.drawdown), now)
    if not admission.get("open"):
        raise TradePlanBlocked(str(admission.get("reason") or "CANONICAL_ENTRY_BLOCKED"))
    return row, admission


def _structural(context, price, direction, now):
    gate = TS.entry_gate(context, float(price), direction, now, config=CTC.STRUCTURAL_ENTRY_POLICY)
    if not gate.get("eligible"):
        raise TradePlanBlocked(str(gate.get("reason") or "BROKER_PRICE_STRUCTURAL_GATE_FAILED"))


def _base(spec, account, quote, now, action, direction, event_id, limit_price):
    reducing = action in ("REDUCE", "CLOSE")
    spec.validate(now)
    account.validate(now, reducing=reducing)
    quote.validate(spec, now)
    if direction not in ("LONG", "SHORT") or not event_id:
        raise TradePlanBlocked("INVALID_TRADE_IDENTITY")
    side = "BUY" if (direction == "LONG") != reducing else "SELL"
    if (side == "BUY" and not spec.buy_available) or (side == "SELL" and not spec.sell_available):
        raise TradePlanBlocked("TRADE_DIRECTION_UNAVAILABLE")
    limit = round_price(limit_price, decimal(spec.tick_size), side)
    if limit <= 0:
        raise TradePlanBlocked("INVALID_LIMIT_PRICE")
    if (side == "BUY" and decimal(quote.ask) > limit) or (side == "SELL" and decimal(quote.bid) < limit):
        raise TradePlanBlocked("PRICE_OUTSIDE_APPROVED_LIMIT")
    return {"portfolio": "Currency", "asset": ASSET, "account_id": account.account_id,
            "instrument_uid": spec.instrument_uid, "action": action, "direction": direction,
            "side": side, "order_type": "LIMIT", "price_type": "POINT",
            "time_in_force": "TIME_IN_FORCE_FILL_AND_KILL", "limit_price": limit,
            "canonical_event_id": event_id, "position_before_lots": account.signed_lots,
            "managed_before_lots": account.managed_signed_lots, "ledger_revision": account.ledger_revision,
            "contract_spec": spec.identity(), "spec_hash": fingerprint(spec.identity()),
            "quote_observed_at": utc(quote.observed_at), "account_observed_at": utc(account.observed_at),
            "prepared_at": utc(now), "plan_version": VERSION, "policy_version": CTC.VERSION}


def prepare_entry(row, admission, spec, account, quote, *, now, action=None, held_terms=None):
    if str(row.get("asset")) != ASSET or admission.get("open") is not True:
        raise TradePlanBlocked("CANONICAL_ADMISSION_REQUIRED")
    plan = dict(admission.get("prepared_plan") or {})
    context = plan.get("timeframe_entry_context") or row.get("timeframe_entry_context") or {}
    event = context.get("event") or {}
    event_id = event.get("event_id") or plan.get("entry_event_id") or plan.get("event_id")
    if not event_id or not plan:
        raise TradePlanBlocked("IMMUTABLE_ENTRY_EVENT_REQUIRED")
    direction = str(plan.get("direction") or event.get("direction") or row.get("research_decision") or row.get("decision"))
    if direction not in ("LONG", "SHORT"):
        raise TradePlanBlocked("INVALID_TRADE_DIRECTION")
    sign = 1 if direction == "LONG" else -1
    if account.signed_lots and account.signed_lots * sign < 0:
        raise TradePlanBlocked("CLOSE_OPPOSITE_POSITION_FIRST")
    required_action = "ADD" if account.signed_lots else "OPEN"
    if action is not None and action != required_action:
        raise TradePlanBlocked("POSITION_ACTION_MISMATCH")
    limit = decimal(quote.ask if sign > 0 else quote.bid, positive=True)
    terms = _base(spec, account, quote, now, required_action, direction, str(event_id), limit)
    _structural(context, terms["limit_price"], direction, now)
    limits = currency_limits()
    if account.drawdown >= limits["hard_drawdown"]:
        raise TradePlanBlocked("CURRENCY_DRAWDOWN_STOP")
    nav = decimal(account.currency_nav_rub, positive=True)
    fraction = min(decimal(admission.get("fraction"), positive=True), limits["max_gross"])
    contract_notional = terms["limit_price"] * spec.rub_per_price_unit_per_lot
    target_lots = int((nav * fraction / contract_notional).to_integral_value(rounding=ROUND_FLOOR))
    lots = target_lots - abs(account.signed_lots)
    if lots < 1:
        raise TradePlanBlocked("TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT")
    source = context.get("source_identity") or context.get("source") or row.get("price_source_identity")
    if not source:
        raise TradePlanBlocked("CANONICAL_SOURCE_IDENTITY_REQUIRED")
    if required_action == "ADD":
        if not held_terms or held_terms.get("horizon") != row.get("horizon"):
            raise TradePlanBlocked("HELD_TIMEFRAME_REQUIRED_FOR_ADD")
        if held_terms.get("source_identity") != json_safe(source):
            raise TradePlanBlocked("HELD_SOURCE_CHANGED")
    levels = held_terms if required_action == "ADD" else plan
    stop = round_price(decimal(levels.get("stop_price"), positive=True), decimal(spec.tick_size),
                       "BUY" if sign > 0 else "SELL")
    target = round_price(decimal(levels.get("target_price"), positive=True), decimal(spec.tick_size),
                         "BUY" if sign > 0 else "SELL")
    price = terms["limit_price"]
    if sign * (price - stop) <= 0 or sign * (target - price) <= 0:
        raise TradePlanBlocked("STRUCTURAL_GEOMETRY_INVALID_AT_BROKER_PRICE")
    spread_bps = (decimal(quote.ask) - decimal(quote.bid)) / price * Decimal("10000")
    exact_plan = dict(plan, direction=direction, entry_price=float(price), stop_price=float(stop),
                      target_price=float(target), expected_move_pct=float(abs(target-price)/price),
                      expected_to_stop_ratio=float(abs(target-price)/abs(price-stop)), spread_bps=float(spread_bps),
                      best_bid=float(quote.bid), best_ask=float(quote.ask))
    economics = VX.economics_gate(ASSET, exact_plan)
    if not economics.get("eligible"):
        raise TradePlanBlocked("BROKER_PRICE_ECONOMICS:" + ",".join(economics.get("blockers") or []))
    margin_per_lot = decimal(spec.margin_buy_rub if sign > 0 else spec.margin_sell_rub, positive=True) * spec.lot_size
    broker_max = account.broker_max_buy_lots if sign > 0 else account.broker_max_sell_lots
    commission_rate = decimal(VX.VC.COMMISSION_RATE)
    reserve = margin_per_lot + contract_notional * commission_rate
    margin_lots = int((max(ZERO, decimal(account.available_margin_rub)) / reserve).to_integral_value(rounding=ROUND_FLOOR))
    lots = min(lots, broker_max, margin_lots)
    if lots < 1:
        raise TradePlanBlocked("INSUFFICIENT_MARGIN_FOR_ONE_CONTRACT")
    exposure_lots = abs(account.signed_lots) + lots
    stop_risk = abs(price - stop) * spec.rub_per_price_unit_per_lot * exposure_lots
    stop_risk += contract_notional * exposure_lots * decimal(economics["modeled_round_trip_cost_pct"])
    cap = decimal(CTC.PAPER_RISK_POLICY["per_idea_structural_stop_risk_cap_nav"])
    if stop_risk > nav * cap:
        raise TradePlanBlocked("FINAL_CONTRACT_STOP_RISK_EXCEEDED")
    terms.update(lots=lots, stop_price=stop, target_price=target, horizon=str(row.get("horizon")),
                 target_fraction=fraction, currency_nav_rub=nav, high_water_rub=decimal(account.high_water_rub),
                 required_margin_rub=margin_per_lot * lots, order_notional_rub=contract_notional * lots,
                 estimated_commission_rub=contract_notional * lots * commission_rate,
                 total_stop_risk_rub=stop_risk, cost_multiple=decimal(economics["minimum_expected_move_pct"]) /
                 decimal(economics["modeled_round_trip_cost_pct"], positive=True),
                 canonical_cost_multiple=decimal(VX.VC.policy(ASSET)["entry_cost_multiple"]), economics=economics,
                 entry_context=context, model_version=str(row.get("model_version") or CTC.VERSION),
                 source_identity=source, broker_execution_source="TINVEST_EXACT_INSTRUMENT", reduce_only=False)
    return json_safe(terms)


def prepare_exit(spec, account, quote, *, now, event_id, reason, lots=None, horizon,
                 stop_price=None, target_price=None, source_identity):
    held, managed = integer(account.signed_lots), integer(account.managed_signed_lots)
    if not held or not managed or held * managed <= 0:
        raise TradePlanBlocked("NO_MANAGED_POSITION_TO_REDUCE")
    if not reason or not source_identity or not horizon:
        raise TradePlanBlocked("EXIT_PROVENANCE_REQUIRED")
    maximum = min(abs(held), abs(managed))
    qty = maximum if lots is None else integer(lots, nonnegative=True)
    if qty < 1 or qty > maximum:
        raise TradePlanBlocked("EXIT_WOULD_EXCEED_MANAGED_POSITION")
    direction = "LONG" if held > 0 else "SHORT"
    action = "CLOSE" if qty == abs(managed) else "REDUCE"
    terms = _base(spec, account, quote, now, action, direction, event_id,
                  decimal(quote.bid if held > 0 else quote.ask))
    terms.update(lots=qty, stop_price=decimal(stop_price, positive=True) if stop_price is not None else None,
                 target_price=decimal(target_price, positive=True) if target_price is not None else None,
                 horizon=horizon, source_identity=source_identity, exit_reason=reason, reduce_only=True,
                 required_margin_rub=ZERO,
                 order_notional_rub=terms["limit_price"] * spec.rub_per_price_unit_per_lot * qty,
                 broker_execution_source="TINVEST_EXACT_INSTRUMENT")
    return json_safe(terms)


def revalidate(terms, spec, account, quote, *, now, canonical_event_valid=False):
    reducing = terms.get("action") in ("CLOSE", "REDUCE")
    spec.validate(now)
    account.validate(now, reducing=reducing)
    quote.validate(spec, now)
    if terms.get("account_id") != account.account_id or terms.get("instrument_uid") != spec.instrument_uid:
        raise TradePlanBlocked("APPROVED_ACCOUNT_OR_INSTRUMENT_CHANGED")
    if terms.get("spec_hash") != fingerprint(spec.identity()):
        raise TradePlanBlocked("CONTRACT_SPEC_CHANGED")
    if integer(terms.get("ledger_revision")) != account.ledger_revision:
        raise TradePlanBlocked("ALLOCATION_LEDGER_CHANGED")
    if integer(terms.get("position_before_lots")) != account.signed_lots:
        raise TradePlanBlocked("POSITION_CHANGED_AFTER_PROPOSAL")
    if integer(terms.get("managed_before_lots")) != account.managed_signed_lots:
        raise TradePlanBlocked("MANAGED_POSITION_CHANGED_AFTER_PROPOSAL")
    side, lots = terms.get("side"), integer(terms.get("lots"), nonnegative=True)
    limit = decimal(terms.get("limit_price"), positive=True)
    if lots < 1 or side not in ("BUY", "SELL"):
        raise TradePlanBlocked("INVALID_APPROVED_ORDER")
    if reducing:
        expected_side = "SELL" if account.signed_lots > 0 else "BUY"
        if (not terms.get("reduce_only") or side != expected_side
                or lots > min(abs(account.signed_lots), abs(account.managed_signed_lots))
                or account.signed_lots * account.managed_signed_lots <= 0):
            raise TradePlanBlocked("EXIT_WOULD_INCREASE_OR_REVERSE_POSITION")
    if (side == "BUY" and not spec.buy_available) or (side == "SELL" and not spec.sell_available):
        raise TradePlanBlocked("TRADE_DIRECTION_UNAVAILABLE")
    if (side == "BUY" and decimal(quote.ask) > limit) or (side == "SELL" and decimal(quote.bid) < limit):
        raise TradePlanBlocked("PRICE_OUTSIDE_APPROVED_LIMIT")
    if reducing:
        return
    if canonical_event_valid is not True:
        raise TradePlanBlocked("CANONICAL_EVENT_NO_LONGER_VALID")
    _structural(terms.get("entry_context") or {}, limit, terms["direction"], now)
    if account.drawdown >= currency_limits()["hard_drawdown"]:
        raise TradePlanBlocked("CURRENCY_DRAWDOWN_STOP")
    margin = decimal(spec.margin_buy_rub if side == "BUY" else spec.margin_sell_rub, positive=True) * spec.lot_size * lots
    if margin > decimal(terms.get("required_margin_rub")):
        raise TradePlanBlocked("MARGIN_INCREASE_REQUIRES_NEW_APPROVAL")
    if margin + decimal(terms.get("estimated_commission_rub")) > decimal(account.available_margin_rub):
        raise TradePlanBlocked("INSUFFICIENT_MARGIN_AFTER_APPROVAL")
    maximum = account.broker_max_buy_lots if side == "BUY" else account.broker_max_sell_lots
    if lots > maximum:
        raise TradePlanBlocked("BROKER_LOT_LIMIT_CHANGED")
    exposure = (abs(account.signed_lots)+lots) * limit * spec.rub_per_price_unit_per_lot
    if exposure > decimal(account.currency_nav_rub) * currency_limits()["max_gross"]:
        raise TradePlanBlocked("ALLOCATION_EXPOSURE_LIMIT_CHANGED")
    stop, target = decimal(terms.get("stop_price"), positive=True), decimal(terms.get("target_price"), positive=True)
    sign = 1 if side == "BUY" else -1
    if sign * (limit-stop) <= 0 or sign * (target-limit) <= 0:
        raise TradePlanBlocked("APPROVED_GEOMETRY_INVALID")
    spread = (decimal(quote.ask)-decimal(quote.bid))/limit*Decimal("10000")
    economics = VX.economics_gate(ASSET, {"direction":terms["direction"], "entry_price":float(limit),
        "stop_price":float(stop), "target_price":float(target), "horizon":terms.get("horizon"),
        "expected_move_pct":float(abs(target-limit)/limit),
        "expected_to_stop_ratio":float(abs(target-limit)/abs(limit-stop)), "spread_bps":float(spread),
        "best_bid":float(quote.bid), "best_ask":float(quote.ask)})
    if not economics.get("eligible"):
        raise TradePlanBlocked("ECONOMICS_CHANGED_AFTER_APPROVAL")
    stop_risk = (abs(limit-stop)/limit + decimal(economics["modeled_round_trip_cost_pct"])) * exposure
    if stop_risk > decimal(account.currency_nav_rub)*decimal(CTC.PAPER_RISK_POLICY["per_idea_structural_stop_risk_cap_nav"]):
        raise TradePlanBlocked("STOP_RISK_CHANGED_AFTER_APPROVAL")
