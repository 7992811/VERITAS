"""Protected, default-off Currency proposal service.

Only explicit configuration enables this module. Constructors do not connect to
a database or broker. A prepared proposal is derived from current broker facts
and the canonical admitted plan, before any order submission. Telegram callbacks
record a decision; the separate coordinator revalidates and claims it before I/O.

Production and sandbox ledgers live in different, constant PostgreSQL schemas.
The dedicated 10,000 RUB allocation never uses paper NAV or total account NAV.
A broker ACK is not an execution. Only complete, uniquely identified POINT
stages plus actual RUB commissions are ingested into the allocation ledger.

Funding completeness is deliberately unresolved after the first actual fill:
an Operations pagination cursor is not a clearing-finality guarantee. New risk
then waits for a future durable settlement reconciler. CLOSE/REDUCE bypass the
entry-only costs gate. No elapsed-time assumption or environment override marks
unknown funding as paid or reconciled.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import os
import re
import threading
import uuid

from veritas_currency_trade_ledger import (
    CurrencyTradeLedger, InstrumentValuation, LedgerError, ACCOUNTS, FILLS, FEES,
)
from veritas_currency_trade_plan import (
    AccountSnapshot, BrokerQuote, ContractSpec, TradePlanBlocked, fresh,
    fingerprint, json_safe, utc,
)
from veritas_currency_trading import CurrencyTradingCoordinator, TradeFacts, TradeOwner
from veritas_tbank_trading import TBankTradingAdapter, ExecutionConfig, TradingError, quotation_to_decimal
from veritas_trade_approvals import TradeApprovals, ApprovalError, PRE_SUBMISSION

PREFIX = "/internal/currency-trading/"
CNY_UID = "c300543d-aa18-4249-b110-615409dde036"
VERSION = "currency-trade-service-v1"
ALLOCATION_RUB = Decimal("10000")
ZERO = Decimal("0")
FULL_ACCESS = "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
_SCHEMAS = {
    "production": "veritas_currency_live_production",
    "sandbox": "veritas_currency_live_sandbox",
}
_CALLBACK = re.compile(r"^ta:[ar]:([a-f0-9]{32}):[A-Za-z0-9_-]{22}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


class ServiceError(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc)


def _number(value, *, positive=False, nonnegative=False):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ServiceError("EXACT_DECIMAL_REQUIRED")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (ValueError, InvalidOperation, TypeError):
        raise ServiceError("INVALID_DECIMAL") from None
    if (not result.is_finite() or abs(result) > Decimal("1e30")
            or (positive and result <= 0) or (nonnegative and result < 0)):
        raise ServiceError("INVALID_DECIMAL")
    return result


def _integer(value, *, positive=False, nonnegative=False):
    number = _number(value, positive=positive, nonnegative=nonnegative)
    if number != number.to_integral_value() or abs(number) > 2**63 - 1:
        raise ServiceError("INTEGER_REQUIRED")
    return int(number)


def _positive_id(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ServiceError("EXPLICIT_POSITIVE_ID_REQUIRED", 400)
    return value


def _text(value, code="INVALID_IDENTIFIER", limit=256):
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > limit:
        raise ServiceError(code, 400)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError(code, 400)
    return value


def _rub(raw, *, nonnegative=False, positive=False):
    if not isinstance(raw, Mapping) or str(raw.get("currency", "")).upper() != "RUB":
        raise ServiceError("EXPLICIT_RUB_VALUE_REQUIRED")
    return _number(quotation_to_decimal(raw), nonnegative=nonnegative, positive=positive)


def _rows(value, code):
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, Mapping) for x in value):
        raise ServiceError(code)
    return value


def _diagnostic(exc, fallback="CANONICAL_ENTRY_BLOCKED"):
    value = getattr(exc, "code", None) or str(exc).split(":", 1)[0]
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else fallback


def _environment_connect(connect, environment):
    """Lazy schema isolation, including ledger DDL; no public fallback."""
    if not callable(connect) or environment not in _SCHEMAS:
        raise ServiceError("INVALID_LEDGER_ENVIRONMENT")
    schema = _SCHEMAS[environment]

    @contextmanager
    def scoped():
        with connect() as connection:
            with connection.transaction():
                # Both identifiers are constants chosen from the closed map.
                connection.execute("CREATE SCHEMA IF NOT EXISTS " + schema)
                connection.execute("SET LOCAL search_path TO " + schema)
                yield connection
    return scoped


@dataclass(frozen=True)
class _BrokerCycle:
    spec: ContractSpec
    quote: BrokerQuote
    observed_at: datetime
    signed_lots: int
    blocked_lots: int
    active_order_count: int
    available_rub: Decimal
    max_buy_lots: int
    max_sell_lots: int
    snapshot_id: str


class BrokerFactsProvider:
    def __init__(self, adapter, ledger, account_id, instrument_uid=CNY_UID,
                 owner_user_id=None, connect=None, clock=None):
        self.adapter, self.ledger = adapter, ledger
        self.account_id = _text(account_id, "EXPLICIT_ACCOUNT_REQUIRED")
        if instrument_uid != CNY_UID:
            raise ServiceError("EXACT_CNY_INSTRUMENT_REQUIRED")
        self.instrument_uid = instrument_uid
        self.owner_user_id = _positive_id(owner_user_id)
        self.connect = connect if connect is not None else ledger.connect
        if not callable(self.connect):
            raise ServiceError("EXPLICIT_LEDGER_CONNECTION_REQUIRED")
        self.clock = clock or _now
        self.environment = adapter.environment
        if self.environment not in _SCHEMAS:
            raise ServiceError("INVALID_EXECUTION_ENVIRONMENT")
        self.block_reason = None

    def _assert_owner(self, row):
        if (row is None or row.get("account_id") != self.account_id
                or row.get("instrument_uid") != self.instrument_uid
                or row.get("owner_user_id") != self.owner_user_id):
            raise ServiceError("CURRENCY_ACCOUNT_BINDING_MISMATCH")

    def is_bound(self):
        with self.connect() as c:
            row = c.execute(
                f"SELECT account_id,instrument_uid,owner_user_id FROM {ACCOUNTS} WHERE account_id=%s",
                (self.account_id,)).fetchone()
        if row is None:
            return False
        self._assert_owner(row)
        return True

    def _spec(self, raw, observed_at):
        if (not isinstance(raw, Mapping) or raw.get("uid") != self.instrument_uid
                or str(raw.get("ticker", "")).upper() != "CNYRUBF"
                or raw.get("realExchange") != "REAL_EXCHANGE_MOEX"
                or str(raw.get("currency", "")).upper() != "RUB"):
            raise ServiceError("EXACT_RUB_CNY_FUTURE_REQUIRED")
        return ContractSpec(
            instrument_uid=self.instrument_uid, ticker="CNYRUBF",
            lot_size=_integer(raw.get("lot"), positive=True),
            tick_size=_number(quotation_to_decimal(raw.get("minPriceIncrement")), positive=True),
            tick_value_rub=_number(quotation_to_decimal(raw.get("minPriceIncrementAmount")), positive=True),
            # Official Future fields 61/62 are MoneyValue; both must be RUB.
            margin_buy_rub=_rub(raw.get("initialMarginOnBuy"), nonnegative=True),
            margin_sell_rub=_rub(raw.get("initialMarginOnSell"), nonnegative=True),
            api_trade_available=raw.get("apiTradeAvailableFlag") is True,
            buy_available=raw.get("buyAvailableFlag") is True,
            sell_available=raw.get("sellAvailableFlag") is True,
            observed_at=observed_at,
        )

    def _position(self, raw, spec):
        if not isinstance(raw, Mapping):
            raise ServiceError("INVALID_POSITIONS_RESPONSE")
        if "accountId" in raw and raw["accountId"] != self.account_id:
            raise ServiceError("BROKER_ACCOUNT_MISMATCH")
        # ProtoJSON omits a default false scalar; explicit non-bools stay invalid.
        if raw.get("limitsLoadingInProgress", False) is not False:
            raise ServiceError("BROKER_LIMITS_NOT_READY")
        matched = []
        for item in _rows(raw.get("futures", []), "INVALID_FUTURES_POSITIONS"):
            uid = item.get("instrumentUid")
            if not uid:
                if _integer(item.get("balance", 0)) or _integer(item.get("blocked", 0)):
                    raise ServiceError("UNIDENTIFIED_FUTURES_POSITION")
                continue
            if uid == self.instrument_uid:
                matched.append(item)
        if len(matched) > 1:
            raise ServiceError("DUPLICATE_CNY_POSITION")
        held, blocked = 0, 0
        if matched:
            balance = _integer(matched[0].get("balance", 0))
            reserved = _integer(matched[0].get("blocked", 0), nonnegative=True)
            if balance % spec.lot_size or reserved % spec.lot_size:
                raise ServiceError("POSITION_NOT_AN_INTEGER_LOT")
            held, blocked = balance // spec.lot_size, reserved // spec.lot_size

        def rub_balance(name):
            found = [x for x in _rows(raw.get(name, []), "INVALID_MONEY_BALANCES")
                     if str(x.get("currency", "")).upper() == "RUB"]
            if len(found) > 1:
                raise ServiceError("DUPLICATE_RUB_BALANCE")
            return _rub(found[0], nonnegative=name == "blocked") if found else ZERO

        return held, blocked, max(ZERO, rub_balance("money") - rub_balance("blocked"))

    def _active_orders(self):
        orders = self.adapter.list_orders(self.account_id)
        stops = self.adapter.list_stop_orders(self.account_id, status="ACTIVE")
        if not isinstance(orders, (list, tuple)) or not isinstance(stops, (list, tuple)):
            raise ServiceError("INVALID_ACTIVE_ORDERS_RESPONSE")
        count = 0
        for order in orders:
            uid = getattr(order, "instrument_uid", None)
            if not uid or uid == self.instrument_uid:
                count += 1
        for order in stops:
            if not isinstance(order, Mapping):
                raise ServiceError("INVALID_ACTIVE_STOP_RESPONSE")
            uid = order.get("instrumentUid")
            if not uid or uid == self.instrument_uid:
                count += 1
        return count

    def _quote(self, raw, status):
        if (not isinstance(raw, Mapping) or raw.get("instrumentUid") != self.instrument_uid
                or not isinstance(status, Mapping) or status.get("instrumentUid") != self.instrument_uid):
            raise ServiceError("BROKER_QUOTE_IDENTITY_MISMATCH")
        if raw.get("isConsistent") is False:
            raise ServiceError("INCONSISTENT_BROKER_ORDER_BOOK")
        bids = _rows(raw.get("bids"), "BROKER_BID_REQUIRED")
        asks = _rows(raw.get("asks"), "BROKER_ASK_REQUIRED")
        if not bids or not asks:
            raise ServiceError("BROKER_BBO_REQUIRED")
        # GetOrderBook uses orderbookTs; streaming time is not a substitute.
        observed = utc(raw.get("orderbookTs"))
        bid = max(_number(quotation_to_decimal(x.get("price")), positive=True)
                  for x in bids if _integer(x.get("quantity"), positive=True))
        ask = min(_number(quotation_to_decimal(x.get("price")), positive=True)
                  for x in asks if _integer(x.get("quantity"), positive=True))
        return BrokerQuote(self.instrument_uid, bid, ask, observed,
                           status.get("limitOrderAvailableFlag") is True
                           and status.get("apiTradeAvailableFlag") is True)

    def _withdrawable_rub(self):
        # GetPositions does not expose collateral reserved for futures.
        # WithdrawLimits supplies separate cash, blocked cash and guarantees.
        # Unknown capacity is zero for entry/bind; exits do not use this value.
        try:
            raw = self.adapter.get_withdraw_limits(self.account_id)
            if not isinstance(raw, Mapping):
                return ZERO
            if "accountId" in raw and raw["accountId"] != self.account_id:
                return ZERO

            def amount(name):
                found = [x for x in _rows(raw.get(name, []), "INVALID_WITHDRAW_LIMITS")
                         if str(x.get("currency", "")).upper() == "RUB"]
                if len(found) > 1:
                    raise ServiceError("DUPLICATE_RUB_WITHDRAW_LIMIT")
                return _rub(found[0], nonnegative=name != "money") if found else ZERO

            return max(ZERO, amount("money") - amount("blocked") - amount("blockedGuarantee"))
        except (TradingError, ServiceError, AttributeError, TypeError):
            return ZERO

    def _max_lots(self, price, side):
        # Missing entry capacity is zero, so it never prevents reducing an
        # existing position through the separate exit path.
        try:
            raw = self.adapter.get_max_lots(self.account_id, self.instrument_uid, price=price)
            prefix, field = ("buy", "buyMaxLots") if side == "BUY" else ("sell", "sellMaxLots")
            chosen = raw.get(prefix + "Limits")
            if getattr(self.adapter.config, "allow_margin", False) is True:
                margin = raw.get(prefix + "MarginLimits")
                if isinstance(margin, Mapping):
                    chosen = margin
            if not isinstance(chosen, Mapping):
                return 0
            return _integer(chosen.get(field), nonnegative=True)
        except (TradingError, ServiceError, AttributeError, TypeError):
            return 0

    def _read(self):
        observed = utc(self.clock())  # START of the cycle, never refreshed at completion.
        accounts = self.adapter.get_accounts()
        matches = [a for a in _rows(accounts, "INVALID_ACCOUNTS_RESPONSE")
                   if a.get("id") == self.account_id]
        if (len(matches) != 1 or matches[0].get("status") != "ACCOUNT_STATUS_OPEN"
                or matches[0].get("accessLevel") != FULL_ACCESS):
            raise ServiceError("EXACT_OPEN_FULL_ACCESS_ACCOUNT_REQUIRED")
        spec = self._spec(self.adapter.get_future(self.instrument_uid), observed)
        signed, blocked, cash = self._position(self.adapter.get_positions(self.account_id), spec)
        cash = min(cash, self._withdrawable_rub())
        active = self._active_orders()
        status = self.adapter.get_trading_status(self.instrument_uid)
        quote = self._quote(self.adapter.get_order_book(self.instrument_uid, depth=1), status)
        buy = self._max_lots(quote.ask, "BUY")
        sell = self._max_lots(quote.bid, "SELL")
        now = utc(self.clock())
        fresh(observed, now, 30, "ACCOUNT_SNAPSHOT_STALE")
        spec.validate(now)
        quote.validate(spec, now)
        return _BrokerCycle(spec, quote, observed, signed, blocked, active,
                            cash, buy, sell, uuid.uuid4().hex)

    def bind(self):
        facts = self._read()
        if (facts.signed_lots or facts.blocked_lots or facts.active_order_count
                or facts.available_rub < ALLOCATION_RUB):
            raise ServiceError("FLAT_UNENCUMBERED_10000_RUB_REQUIRED")
        return self.ledger.account_bind(
            self.account_id, self.instrument_uid, owner_user_id=self.owner_user_id,
            spec=InstrumentValuation.from_contract(facts.spec),
            broker_signed_lots=0, broker_snapshot_id=facts.snapshot_id,
            observed_at=facts.observed_at, verified=True, broker_open_order_count=0,
            allocation_rub=ALLOCATION_RUB,
        )

    def __call__(self):
        self.block_reason = None
        facts = self._read()
        if facts.blocked_lots:
            # The broker labels balance as unblocked holdings. Do not mistake
            # a reservation for an external position change and latch a freeze.
            self.block_reason = "WORKING_ORDER_RECONCILIATION_REQUIRED"
            raise ServiceError(self.block_reason)
        valuation = InstrumentValuation.from_contract(facts.spec)
        with self.connect() as c:
            with c.transaction():
                owner = c.execute(
                    f"SELECT account_id,instrument_uid,owner_user_id FROM {ACCOUNTS} "
                    "WHERE account_id=%s FOR UPDATE", (self.account_id,)).fetchone()
                if owner is None:
                    raise ServiceError("CURRENCY_ACCOUNT_NOT_BOUND")
                self._assert_owner(owner)
                state = self.ledger.reconcile_broker_positions_on(
                    c, self.account_id, self.instrument_uid, broker_signed_lots=facts.signed_lots,
                    broker_snapshot_id=facts.snapshot_id, observed_at=facts.observed_at,
                    verified=True, broker_open_order_count=facts.active_order_count,
                )
                mark = facts.quote.ask if state["signed_lots"] < 0 else facts.quote.bid
                state = self.ledger.snapshot_on(
                    c, self.account_id, self.instrument_uid, mark_price=mark,
                    mark_observed_at=facts.quote.observed_at, spec=valuation,
                )
                prior_fill = c.execute(
                    f"SELECT trade_id FROM {FILLS} WHERE account_id=%s AND instrument_uid=%s LIMIT 1",
                    (self.account_id, self.instrument_uid)).fetchone()
        funding_reconciled = prior_fill is None
        costs = state.get("costs_reconciled") is True and funding_reconciled
        if state.get("entries_frozen"):
            self.block_reason = "BROKER_POSITION_MISMATCH"
        elif not funding_reconciled:
            self.block_reason = "FUNDING_COMPLETENESS_UNVERIFIED"
        elif not state.get("costs_reconciled"):
            self.block_reason = "BROKER_COST_RECONCILIATION_REQUIRED"
        account = AccountSnapshot(
            account_id=self.account_id, access_level=FULL_ACCESS,
            currency_nav_rub=_number(state["currency_nav_rub"]),
            high_water_rub=_number(state["high_water_rub"], positive=True),
            available_margin_rub=facts.available_rub, signed_lots=facts.signed_lots,
            managed_signed_lots=_integer(state["managed_signed_lots"]),
            blocked_lots=facts.blocked_lots, active_order_count=facts.active_order_count,
            broker_max_buy_lots=facts.max_buy_lots, broker_max_sell_lots=facts.max_sell_lots,
            ledger_revision=_integer(state["ledger_revision"], nonnegative=True),
            observed_at=facts.observed_at, reconciled=state.get("reconciled") is True,
            costs_reconciled=costs,
        )
        return TradeFacts(facts.spec, account, facts.quote, state.get("held_terms"))

    def ingest(self, proposal, result):
        terms = proposal.get("terms") or {}
        if (proposal.get("account_id") != self.account_id
                or terms.get("account_id") != self.account_id
                or proposal.get("instrument_uid") != self.instrument_uid
                or terms.get("instrument_uid") != self.instrument_uid
                or proposal.get("owner_user_id") != self.owner_user_id
                or terms.get("execution_environment") != self.environment
                or terms.get("portfolio") != "Currency" or terms.get("asset") != "CNYRUBF"):
            raise ServiceError("EXECUTION_SCOPE_MISMATCH")
        client = _text(proposal.get("client_order_id"), "CLIENT_ORDER_ID_REQUIRED")
        broker = _text(result.broker_order_id, "BROKER_ORDER_ID_REQUIRED")
        if (result.instrument_uid != self.instrument_uid or result.side != terms.get("side")
                or result.lots_requested != _integer(terms.get("lots"), positive=True)
                or result.client_order_id not in (None, client)
                or (proposal.get("broker_order_id") and proposal["broker_order_id"] != broker)
                or (result.client_order_id is None and not proposal.get("broker_order_id"))):
            raise ServiceError("EXECUTION_IDENTITY_MISMATCH")
        total = _integer(result.lots_executed, nonnegative=True)
        if total > _integer(result.lots_requested, positive=True):
            raise ServiceError("EXECUTIONS_EXCEED_APPROVED_LOTS")
        unique = {}
        for fill in result.executions:
            trade_id = _text(fill.trade_id, "BROKER_TRADE_ID_REQUIRED")
            if trade_id in unique and unique[trade_id] != fill:
                raise ServiceError("CONFLICTING_EXECUTION_STAGE")
            if (fill.price_type != "POINT" or fill.currency != "RUB"
                    or _integer(fill.lots, positive=True) <= 0):
                raise ServiceError("EXACT_RUB_POINT_EXECUTION_REQUIRED")
            unique[trade_id] = fill
        if sum(x.lots for x in unique.values()) != total:
            raise ServiceError("COMPLETE_EXECUTION_STAGES_REQUIRED")
        commission = result.executed_commission
        if not total:
            if unique or (commission is not None and _number(commission) != ZERO):
                raise ServiceError("ZERO_FILL_RECEIPT_HAS_COST_OR_EXECUTIONS")
            return {"status": "NO_EXECUTIONS", "filled_lots": 0}
        if commission is None or result.commission_currency != "RUB":
            raise ServiceError("ACTUAL_RUB_COMMISSION_REQUIRED")
        commission = _number(commission, nonnegative=True)
        spec = InstrumentValuation.from_contract(terms.get("contract_spec") or {})
        if spec.instrument_uid != self.instrument_uid:
            raise ServiceError("EXECUTION_VALUATION_SCOPE_MISMATCH")
        observed = utc(result.observed_at)
        fee_id = fingerprint({
            "account_id": self.account_id, "instrument_uid": self.instrument_uid,
            "client_order_id": client, "broker_order_id": broker, "filled_lots": total,
            "fee_rub": format(commission.normalize(), "f"),
        })
        with self.connect() as c:
            with c.transaction():
                owner = c.execute(
                    f"SELECT account_id,instrument_uid,owner_user_id FROM {ACCOUNTS} "
                    "WHERE account_id=%s FOR UPDATE", (self.account_id,)).fetchone()
                self._assert_owner(owner)
                for fill in sorted(unique.values(), key=lambda x: (utc(x.executed_at), x.trade_id)):
                    self.ledger.record_fill_on(
                        c, self.account_id, self.instrument_uid, trade_id=fill.trade_id,
                        client_order_id=client, broker_order_id=broker, side=result.side,
                        lots_count=fill.lots, price=_number(fill.price, positive=True),
                        executed_at=utc(fill.executed_at), spec=spec, price_type="POINT",
                        currency="RUB", metadata=terms,
                    )
                # Stable observation IDs reuse their first observed timestamp;
                # repeated polls cannot conflict merely because time advanced.
                previous = c.execute(
                    f"SELECT observed_at FROM {FEES} WHERE account_id=%s AND instrument_uid=%s "
                    "AND observation_id=%s", (self.account_id, self.instrument_uid, fee_id)).fetchone()
                state = self.ledger.record_order_fee_on(
                    c, self.account_id, self.instrument_uid, observation_id=fee_id,
                    client_order_id=client, broker_order_id=broker,
                    cumulative_fee_rub=commission, filled_lots=total,
                    observed_at=previous["observed_at"] if previous else observed, currency="RUB",
                )
        return state


def _key_bytes(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or len(value) < 32:
        raise ServiceError("TRADE_SERVICE_KEY_NOT_CONFIGURED", 503)
    return value


def _authenticate(headers, key):
    try:
        values = [v for k, v in headers.items() if str(k).lower() == "x-veritas-trade-key"]
    except (AttributeError, TypeError):
        values = []
    if (len(values) != 1 or not isinstance(values[0], str)
            or not hmac.compare_digest(values[0].encode("utf-8"), _key_bytes(key))):
        raise ServiceError("TRADE_SERVICE_AUTH_REQUIRED", 401)


def _error(exc):
    if isinstance(exc, (ServiceError, ApprovalError)):
        code = _diagnostic(exc, "TRADE_REQUEST_REJECTED")
        status = getattr(exc, "status_code", 409)
        return {"ok": False, "code": code}, status if status in (400, 401, 403, 404, 409, 410, 429, 503) else 409
    if isinstance(exc, (TradePlanBlocked, LedgerError)):
        return {"ok": False, "code": _diagnostic(exc)}, 409
    if isinstance(exc, TradingError):
        return {"ok": False, "code": "BROKER_FACTS_UNAVAILABLE"}, 503
    return {"ok": False, "code": "TRADE_SERVICE_TEMPORARILY_UNAVAILABLE"}, 503


class TradeHttpApplication:
    def __init__(self, *, repository, coordinator, facts, owner, service_key, ledger=None):
        if not isinstance(owner, TradeOwner):
            raise ServiceError("EXPLICIT_TRADE_OWNER_REQUIRED")
        self.repository, self.coordinator, self.facts = repository, coordinator, facts
        self.owner, self.ledger = owner, ledger if ledger is not None else facts.ledger
        self.account_id = _text(coordinator.account_id, "EXPLICIT_ACCOUNT_REQUIRED")
        self.instrument_uid = CNY_UID
        self.environment = coordinator.adapter.environment
        if self.environment not in _SCHEMAS:
            raise ServiceError("INVALID_EXECUTION_ENVIRONMENT")
        self._key = _key_bytes(service_key)
        self._ready = False
        self._lock = threading.RLock()

    @property
    def execution_enabled(self):
        return self.coordinator.execution_enabled is True

    def _initialize(self):
        if not self._ready:
            self.repository.ensure_schema()
            self.ledger.ensure_schema()
            self._ready = True

    def _body(self, body):
        if not isinstance(body, Mapping):
            raise ServiceError("JSON_OBJECT_REQUIRED", 400)
        if _positive_id(body.get("bot_id")) != self.owner.bot_id:
            raise ServiceError("TRADE_BOT_BINDING_MISMATCH", 403)
        for key, expected in (("account_id", self.account_id),
                              ("instrument_uid", self.instrument_uid),
                              ("execution_environment", self.environment)):
            if key in body and body[key] != expected:
                raise ServiceError("TRADE_REQUEST_SCOPE_MISMATCH", 403)

    def _private_owner(self, body):
        if (body.get("chat_type") != "private"
                or _positive_id(body.get("sender_user_id")) != self.owner.user_id
                or _positive_id(body.get("private_chat_id")) != self.owner.private_chat_id):
            raise ServiceError("PRIVATE_TRADE_OWNER_REQUIRED", 403)

    def _belongs(self, proposal):
        if not isinstance(proposal, Mapping):
            return False
        terms = proposal.get("terms") or {}
        return (
            proposal.get("account_id") == self.account_id
            and proposal.get("instrument_uid") == self.instrument_uid
            and proposal.get("owner_user_id") == self.owner.user_id
            and proposal.get("private_chat_id") == self.owner.private_chat_id
            and proposal.get("bot_id") == self.owner.bot_id
            and terms.get("account_id") == self.account_id
            and terms.get("instrument_uid") == self.instrument_uid
            and terms.get("execution_environment") == self.environment
            and terms.get("portfolio") == "Currency" and terms.get("asset") == "CNYRUBF"
        )

    def _get(self, proposal_id):
        proposal = self.repository.get(_text(proposal_id, "PROPOSAL_ID_REQUIRED", 64))
        if proposal is None:
            raise ServiceError("TRADE_PROPOSAL_NOT_FOUND", 404)
        if not self._belongs(proposal):
            raise ServiceError("TRADE_PROPOSAL_SCOPE_MISMATCH", 403)
        return proposal

    def _public(self, proposal, *, delivery=False):
        if proposal is None:
            return None
        if not self._belongs(proposal):
            raise ServiceError("TRADE_PROPOSAL_SCOPE_MISMATCH", 403)
        hidden = {"claim_token", "worker_id", "terms_signature", "callback_nonce", "terms_json"}
        if not delivery:
            hidden.add("delivery_token")
        return json_safe({k: v for k, v in proposal.items() if k not in hidden})

    def _scoped(self, records):
        return [p for p in records if self._belongs(p)]

    def _pending(self):
        return self._scoped(self.repository.list_by_status(
            PRE_SUBMISSION, account_id=self.account_id, owner_user_id=self.owner.user_id, limit=1000))

    def _unsettled(self):
        return self._scoped(self.repository.list_unsettled(
            account_id=self.account_id, owner_user_id=self.owner.user_id, limit=1000))

    def _poll(self):
        self.repository.expire()
        if not self.facts.is_bound():
            return {"ok": True, "enabled": True, "items": [],
                    "execution_enabled": self.execution_enabled, "block_reason": "CURRENCY_ACCOUNT_NOT_BOUND"}
        self.coordinator.reconcile()
        reason = None
        approved = self._scoped(self.repository.list_approved(
            account_id=self.account_id, owner_user_id=self.owner.user_id, limit=1000))
        if approved:
            if self.execution_enabled:
                # The coordinator revalidates facts and atomically claims before
                # I/O. A second worker cannot send the same approval.
                self.coordinator.execute_approved(approved[0]["proposal_id"])
            else:
                reason = "CURRENCY_TRADE_EXECUTION_DISABLED"
        unsettled, pending = self._unsettled(), self._pending()
        if unsettled:
            reason = "EXECUTION_RECONCILIATION_PENDING"
        elif not pending:
            try:
                self.coordinator.prepare_next()
            except (TradePlanBlocked, ServiceError, LedgerError) as exc:
                reason = _diagnostic(exc)
                if reason == "BROKER_COST_RECONCILIATION_REQUIRED":
                    reason = self.facts.block_reason or reason
            pending = self._pending()
        items = [self._public(p) for p in pending if p.get("status") == "PENDING_DELIVERY"][:1]
        return {"ok": True, "enabled": True, "items": items,
                "execution_enabled": self.execution_enabled, "block_reason": reason}

    def handle(self, path, body, headers):
        try:
            _authenticate(headers, self._key)
            if not isinstance(path, str) or not path.startswith(PREFIX):
                raise ServiceError("TRADE_ENDPOINT_NOT_FOUND", 404)
            operation = path[len(PREFIX):]
            if operation not in {"status", "bind", "decision", "claim-delivery",
                                 "delivered", "delivery-unknown", "updates", "poll"}:
                raise ServiceError("TRADE_ENDPOINT_NOT_FOUND", 404)
            self._body(body)
            if operation in {"bind", "decision"}:
                self._private_owner(body)
            if operation == "delivered":
                if _positive_id(body.get("private_chat_id")) != self.owner.private_chat_id:
                    raise ServiceError("PRIVATE_TRADE_OWNER_REQUIRED", 403)
            if operation == "status":
                return {"ok": True, "enabled": True, "version": VERSION,
                        "execution_enabled": self.execution_enabled,
                        "account_id": self.account_id, "instrument_uid": self.instrument_uid,
                        "execution_environment": self.environment,
                        "live_account_admission_configured": callable(getattr(self.coordinator, "live_admission", None)),
                        "new_risk_block_reason": ("LIVE_ACCOUNT_ADMISSION_REQUIRED"
                            if self.environment == "production" and not callable(getattr(self.coordinator, "live_admission", None))
                            else None)}, 200
            with self._lock:
                self._initialize()
                if operation == "bind":
                    return {"ok": True, "binding": json_safe(self.facts.bind())}, 200
                if operation == "poll":
                    return self._poll(), 200
                if operation == "updates":
                    limit = _integer(body.get("limit", 20), positive=True)
                    if limit > 100:
                        raise ServiceError("INVALID_LIST_LIMIT", 400)
                    since = utc(body["updated_after"]) if body.get("updated_after") is not None else None
                    rows = self.repository.list_recent(
                        account_id=self.account_id, owner_user_id=self.owner.user_id,
                        instrument_uid=self.instrument_uid, execution_environment=self.environment,
                        limit=limit, updated_after=since)
                    return {"ok": True, "items": [self._public(p) for p in self._scoped(rows)],
                            "execution_enabled": self.execution_enabled}, 200
                if operation == "decision":
                    callback = body.get("callback_data")
                    match = _CALLBACK.fullmatch(callback) if isinstance(callback, str) else None
                    if match is None:
                        raise ServiceError("INVALID_TRADE_CALLBACK", 400)
                    self._get(match.group(1))
                    record = self.repository.decide(
                        callback, sender_user_id=self.owner.user_id,
                        private_chat_id=self.owner.private_chat_id,
                        message_id=_positive_id(body.get("message_id")), bot_id=self.owner.bot_id,
                        callback_query_id=_text(body.get("callback_query_id"), "CALLBACK_QUERY_ID_REQUIRED"))
                    return {"ok": True, "proposal": self._public(record)}, 200
                proposal = self._get(body.get("proposal_id"))
                proposal_id = proposal["proposal_id"]
                if operation == "claim-delivery":
                    record = self.repository.claim_delivery(
                        proposal_id, _text(body.get("worker_id"), "DELIVERY_WORKER_REQUIRED", 128))
                    return {"ok": True, "proposal": self._public(record, delivery=True)}, 200
                token = _text(body.get("delivery_token"), "DELIVERY_TOKEN_REQUIRED", 128)
                if operation == "delivered":
                    self.repository.mark_delivered(
                        proposal_id, bot_id=self.owner.bot_id, private_chat_id=self.owner.private_chat_id,
                        message_id=_positive_id(body.get("message_id")),
                        terms_hash=_text(body.get("terms_hash"), "TERMS_HASH_REQUIRED", 64),
                        delivery_token=token)
                    return {"ok": True}, 200
                self.repository.record_delivery_unknown(proposal_id, token)
                return {"ok": True}, 200
        except Exception as exc:
            return _error(exc)


def _enabled(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _configured_id(name):
    raw = os.environ.get(name, "")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        raise ServiceError("EXPLICIT_TRADE_OWNER_CONFIGURATION_REQUIRED", 503)
    return _positive_id(int(raw))


def create_application(connect, summary_provider):
    """Construct only; the authenticated application initializes lazily."""
    if not _enabled("VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED"):
        return None
    service_key = _key_bytes(os.environ.get("VERITAS_CURRENCY_TRADE_SERVICE_KEY", ""))
    approval_key = _key_bytes(os.environ.get("VERITAS_CURRENCY_TRADE_APPROVAL_KEY", ""))
    if hmac.compare_digest(service_key, approval_key):
        raise ServiceError("DISTINCT_TRADE_SERVICE_AND_APPROVAL_KEYS_REQUIRED", 503)
    owner = TradeOwner(
        _configured_id("VERITAS_CURRENCY_TRADE_OWNER_USER_ID"),
        _configured_id("VERITAS_CURRENCY_TRADE_OWNER_CHAT_ID"),
        _configured_id("VERITAS_CURRENCY_TRADE_BOT_ID"),
    )
    account = _text(os.environ.get("TBANK_ACCOUNT_ID", ""), "EXPLICIT_ACCOUNT_REQUIRED")
    environment = os.environ.get("VERITAS_CURRENCY_TRADE_ENVIRONMENT", "production")
    if environment not in _SCHEMAS:
        raise ServiceError("INVALID_EXECUTION_ENVIRONMENT", 503)
    token_name = "TBANK_API_TOKEN" if environment == "production" else "TBANK_SANDBOX_TOKEN"
    token = _text(os.environ.get(token_name, ""), "EXPLICIT_BROKER_TOKEN_REQUIRED", 4096)
    enabled = (_enabled("VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED")
               and _enabled("VERITAS_LIVE_EXECUTION_ENABLED"))
    armed = _enabled("VERITAS_LIVE_EXECUTION_ARMED")
    if not callable(connect) or not callable(summary_provider):
        raise ServiceError("EXPLICIT_SERVICE_DEPENDENCIES_REQUIRED", 503)
    adapter = TBankTradingAdapter(token, config=ExecutionConfig(
        enabled=enabled, armed=armed,
        allowed_account_ids=frozenset({account}), allowed_instrument_uids=frozenset({CNY_UID}),
        allow_stop_orders=False, allow_margin=_enabled("VERITAS_CURRENCY_TRADE_MARGIN_ALLOWED"),
        environment=environment,
    ))
    ledger_connect = _environment_connect(connect, environment)
    ledger = CurrencyTradeLedger(ledger_connect, enabled=True)
    repository = TradeApprovals(connect, approval_key)
    facts = BrokerFactsProvider(adapter, ledger, account, CNY_UID, owner.user_id, ledger_connect)
    coordinator = CurrencyTradingCoordinator(
        repository=repository, adapter=adapter, account_id=account, owner=owner,
        facts=facts, summary=summary_provider, ingest_execution=facts.ingest,
        execution_enabled=enabled and armed,
        # Whole-account risk/promotion/calibration evidence is not wired yet.
        # Flags and owner approval cannot replace the existing live authority.
        live_admission=None,
    )
    return TradeHttpApplication(
        repository=repository, coordinator=coordinator, facts=facts,
        owner=owner, service_key=service_key, ledger=ledger,
    )


_CONFIG_NAMES = (
    "VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED", "TBANK_ACCOUNT_ID",
    "VERITAS_CURRENCY_TRADE_OWNER_USER_ID", "VERITAS_CURRENCY_TRADE_OWNER_CHAT_ID",
    "VERITAS_CURRENCY_TRADE_BOT_ID", "VERITAS_CURRENCY_TRADE_SERVICE_KEY",
    "VERITAS_CURRENCY_TRADE_APPROVAL_KEY", "VERITAS_CURRENCY_TRADE_ENVIRONMENT",
    "TBANK_API_TOKEN", "TBANK_SANDBOX_TOKEN", "VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED",
    "VERITAS_LIVE_EXECUTION_ENABLED", "VERITAS_LIVE_EXECUTION_ARMED",
    "VERITAS_CURRENCY_TRADE_MARGIN_ALLOWED",
)
_CACHE = {}
_CACHE_LOCK = threading.RLock()


def handle_request(path, body, headers, connect, summary_provider):
    """Return (JSON payload, HTTP status); authenticate before configuration/DB."""
    try:
        if not isinstance(path, str) or not path.startswith(PREFIX):
            return {"ok": False, "code": "TRADE_ENDPOINT_NOT_FOUND"}, 404
        if not _enabled("VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED"):
            return {"ok": True, "enabled": False, "items": [],
                    "execution_enabled": False, "block_reason": "CURRENCY_TRADE_PROPOSALS_DISABLED"}, 200
        key = _key_bytes(os.environ.get("VERITAS_CURRENCY_TRADE_SERVICE_KEY", ""))
        _authenticate(headers, key)
        # A secret/configuration change creates a new inert instance. The digest
        # is local only; credentials and fingerprints never appear in responses.
        config_hash = hashlib.sha256(
            "\0".join(os.environ.get(name, "") for name in _CONFIG_NAMES).encode("utf-8")).digest()
        cache_key = (id(connect), id(summary_provider), config_hash)
        with _CACHE_LOCK:
            application = _CACHE.get(cache_key)
            if application is None:
                application = create_application(connect, summary_provider)
                if application is None:
                    return {"ok": True, "enabled": False, "items": [],
                            "execution_enabled": False, "block_reason": "CURRENCY_TRADE_PROPOSALS_DISABLED"}, 200
                _CACHE.clear()
                _CACHE[cache_key] = application
        return application.handle(path, body, headers)
    except Exception as exc:
        return _error(exc)
