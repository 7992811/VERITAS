"""Owner-specified orders. Pure preparation; no broker or model I/O.

This is a separate decision authority, never a fabricated canonical event.
The approval repository's legacy event/model columns carry explicit MANUAL
identities. Native signal proofs and model admission are not produced here.
"""
from datetime import timedelta
import uuid

import veritas_currency_trade_plan as P
import veritas_execution as VX
import veritas_price_source as VPS

VERSION = "OWNER_MANUAL_TRIAL_V1"
PLAN_VERSION = "currency-owner-manual-v4-limit-day-risk-only"


def applies(terms):
    return isinstance(terms, dict) and terms.get("decision_authority") == VERSION


def request(value):
    if not isinstance(value, dict):
        raise P.TradePlanBlocked("MANUAL_PARAMETERS_REQUIRED")
    closing = value.get("action") == "CLOSE"
    required = {"request_id", "action", "limit_price"} if closing else {
        "request_id", "action", "side", "lots", "limit_price", "stop_price", "target_price", "hold_minutes"}
    if set(value) != required or value.get("action") not in ("OPEN", "CLOSE"):
        raise P.TradePlanBlocked("MANUAL_PARAMETERS_REQUIRED")
    try:
        rid = str(uuid.UUID(value["request_id"]))
        if rid != value["request_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise P.TradePlanBlocked("MANUAL_REQUEST_ID_REQUIRED") from None
    result = {"request_id": rid, "action": value["action"]}
    for key in ("limit_price",) if closing else ("limit_price", "stop_price", "target_price"):
        if isinstance(value[key], float) or len(str(value[key])) > 64:
            raise P.TradePlanBlocked("MANUAL_PARAMETERS_REQUIRED")
        result[key] = P.text_number(P.decimal(value[key], positive=True).normalize())
    if not closing:
        if value["side"] not in ("BUY", "SELL"):
            raise P.TradePlanBlocked("MANUAL_SIDE_REQUIRED")
        if type(value["lots"]) is not int or value["lots"] < 1:
            raise P.TradePlanBlocked("MANUAL_POSITIVE_LOTS_REQUIRED")
        minutes = value["hold_minutes"]
        if type(minutes) is not int or minutes < 1:
            raise P.TradePlanBlocked("MANUAL_HOLD_MINUTES_REQUIRED")
        result.update(side=value["side"], lots=value["lots"], hold_minutes=minutes)
        sign = 1 if value["side"] == "BUY" else -1
        limit, stop, target = (P.decimal(result[k]) for k in ("limit_price", "stop_price", "target_price"))
        if sign * (limit-stop) <= 0 or sign * (target-limit) <= 0:
            raise P.TradePlanBlocked("MANUAL_GEOMETRY_INVALID")
    return result


def event_id(value):
    return "MANUAL-" + request(value)["request_id"]


def validate_intent(terms, spec, account, quote, now):
    """Check explicit immutable intent; never infer entry permission from a tag."""
    if not applies(terms) or terms.get("plan_version") != PLAN_VERSION:
        raise P.TradePlanBlocked("MANUAL_PLAN_VERSION_REQUIRED")
    if terms.get("policy_version") != P.CTC.VERSION:
        raise P.TradePlanBlocked("PLAN_VERSION_REQUIRES_NEW_APPROVAL")
    if terms.get('time_in_force') != 'TIME_IN_FORCE_DAY':
        raise P.TradePlanBlocked('MANUAL_APPROVED_TERMS_CHANGED')
    intent = request(terms.get("manual_request"))
    action = 'ADD' if intent['action'] == 'OPEN' and account.signed_lots else intent['action']
    if (action != terms.get("action") or event_id(intent) != terms.get("canonical_event_id")
            or terms.get("manual_request_hash") != P.fingerprint(intent)
            or P.decimal(intent["limit_price"]) != P.decimal(terms.get("limit_price"))):
        raise P.TradePlanBlocked("MANUAL_APPROVED_TERMS_CHANGED")
    deadline = P.utc(terms.get("manual_intent_expires_at"))
    prepared = P.utc(terms.get("prepared_at"))
    if not prepared <= P.utc(now) < deadline <= prepared + timedelta(seconds=300):
        raise P.TradePlanBlocked("MANUAL_INTENT_EXPIRED")
    if terms.get("action") == "CLOSE":
        return
    expected_source = VPS.identity("CNYRUBF", P._broker_quote(spec, quote, terms.get("direction")))
    if (terms.get("horizon") != "MANUAL" or terms.get("model_version") != VERSION
            or terms.get("source_identity") != expected_source
            or terms.get("entry_context") is not None or terms.get("entry_context_json") is not None
            or terms.get("reduce_only") is not False
            or terms.get("side") != intent["side"]
            or terms.get("direction") != ("LONG" if intent["side"] == "BUY" else "SHORT")
            or terms.get("lots") != intent["lots"]
            or terms.get("expected_hold_seconds") != str(intent["hold_minutes"] * 60)):
        raise P.TradePlanBlocked("MANUAL_APPROVED_TERMS_CHANGED")
    sign = 1 if intent["side"] == "BUY" else -1
    if account.signed_lots * sign < 0:
        raise P.TradePlanBlocked("MANUAL_REVERSE_REQUIRES_CLOSE")
    for key in ("limit_price", "stop_price", "target_price"):
        price = P.decimal(terms.get(key), positive=True)
        if price != P.decimal(intent[key]) or price % spec.tick_size:
            raise P.TradePlanBlocked("MANUAL_APPROVED_PRICE_OFF_TICK")


def prepare(value, facts, now, *, ttl_seconds=120):
    intent = request(value)
    spec, account, quote = facts.spec, facts.account, facts.quote
    price = P.decimal(intent["limit_price"], positive=True)
    if price % spec.tick_size:
        raise P.TradePlanBlocked("MANUAL_APPROVED_PRICE_OFF_TICK")
    held = facts.held_terms or {}
    if intent["action"] == "CLOSE":
        if held.get("decision_authority") != VERSION or not account.managed_signed_lots:
            raise P.TradePlanBlocked("MANUAL_POSITION_REQUIRED")
        terms = P.prepare_exit(spec, account, quote, now=now, event_id=event_id(intent),
            reason="OWNER_MANUAL_CLOSE", lots=abs(account.managed_signed_lots), horizon=held.get("horizon"),
            stop_price=held.get("stop_price"), target_price=held.get("target_price"),
            source_identity=held.get("source_identity"))
        terms.update(limit_price=price, order_notional_rub=price * spec.rub_per_price_unit_per_lot * terms['lots'],
                     time_in_force='TIME_IN_FORCE_DAY')
    else:
        direction = "LONG" if intent["side"] == "BUY" else "SHORT"
        action = "ADD" if account.signed_lots else "OPEN"
        if action == "ADD" and (held.get("decision_authority") != VERSION
                or any(P.decimal(held.get(k)) != P.decimal(intent[k]) for k in ("stop_price", "target_price"))):
            raise P.TradePlanBlocked("MANUAL_ADD_REQUIRES_HELD_LEVELS")
        terms = P._base(spec, account, quote, now, action, direction, event_id(intent), price,
                        time_in_force='TIME_IN_FORCE_DAY')
        stop, target = (P.decimal(intent[k], positive=True) for k in ("stop_price", "target_price"))
        lots, total_lots = intent['lots'], abs(account.signed_lots) + intent['lots']
        notional = price * spec.rub_per_price_unit_per_lot * total_lots
        nav = P.decimal(account.currency_nav_rub, positive=True)
        fraction = notional / nav
        plan = P.live_economics_plan(direction, price, stop, target, quote, "MANUAL",
            fraction=fraction, expected_hold_seconds=intent["hold_minutes"] * 60, owner_limit=True)
        economics = VX.economics_gate("CNYRUBF", plan, execution_mode="LIVE", now=now)
        # Economics still supplies real modeled costs; the owner chooses reward/risk.
        margin = P.decimal(spec.margin_buy_rub if intent["side"] == "BUY" else spec.margin_sell_rub) * spec.lot_size * lots
        order_notional = price * spec.rub_per_price_unit_per_lot * lots
        risk = abs(price-stop) * spec.rub_per_price_unit_per_lot * total_lots + notional * P.decimal(economics["modeled_round_trip_cost_pct"], positive=True)
        terms.update(lots=lots, stop_price=stop, target_price=target, horizon="MANUAL", model_version=VERSION,
            expected_hold_seconds=str(intent["hold_minutes"] * 60), target_fraction=fraction,
            currency_nav_rub=nav, high_water_rub=account.high_water_rub, sizing_mode="OWNER_EXACT_CONTRACT_QUANTITY",
            resulting_position_lots=total_lots, resulting_notional_rub=notional, resulting_leverage=fraction,
            required_margin_rub=margin, order_notional_rub=order_notional,
            estimated_commission_rub=order_notional * P.decimal(VX.VC.COMMISSION_RATE), total_stop_risk_rub=risk,
            economics_mode="LIVE", target_execution_policy="SINGLE_TARGET_SEPARATE_CONFIRMATION",
            source_identity=VPS.identity("CNYRUBF", P._broker_quote(spec, quote, direction)),
            broker_execution_source="TINVEST_EXACT_INSTRUMENT", reduce_only=False)
    terms.update(decision_authority=VERSION, plan_version=PLAN_VERSION,
        manual_request=intent, manual_request_hash=P.fingerprint(intent),
        manual_intent_expires_at=P.utc(now) + timedelta(seconds=ttl_seconds),
        protective_order_mode="EXIT_REQUIRES_SEPARATE_CONFIRMATION")
    terms = P.json_safe(terms)
    validate_intent(terms, spec, account, quote, now)
    P.revalidate(terms, spec, account, quote, now=now)
    return terms
