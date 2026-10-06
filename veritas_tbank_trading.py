"""Explicit, disabled-by-default T-Invest REST adapter.

No environment variables, connections or orders are read/created at import.
The caller must durably record a SENDING claim and its UUID before a mutation;
the injectable journal is a second guard, not a replacement for that transaction.
An uncertain mutation is never retried. Reconciliation only calls read methods.

Official schemas checked 2026-10-07:
https://developer.tbank.ru/invest/services/orders/methods
https://developer.tbank.ru/invest/services/stop-orders/stoporders
https://developer.tbank.ru/invest/intro/developer/protocols/
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import re
import ssl
import threading
import time
from typing import Any
from uuid import UUID


VERSION = "TBANK_REST_EXPLICIT_TRADING_V1"
ROOTS = {"production": "https://invest-public-api.tbank.ru/rest/",
         "sandbox": "https://sandbox-invest-public-api.tbank.ru/rest/"}
PACKAGE = "tinkoff.public.invest.api.contract.v1."
READ_METHODS = {
    "accounts": "UsersService/GetAccounts",
    "future": "InstrumentsService/FutureBy",
    "portfolio": "OperationsService/GetPortfolio",
    "positions": "OperationsService/GetPositions",
    "withdraw_limits": "OperationsService/GetWithdrawLimits",
    "orders": "OrdersService/GetOrders",
    "order": "OrdersService/GetOrderState",
    "max_lots": "OrdersService/GetMaxLots",
    "order_price": "OrdersService/GetOrderPrice",
    "stop_orders": "StopOrdersService/GetStopOrders",
    "book": "MarketDataService/GetOrderBook",
    "trading_status": "MarketDataService/GetTradingStatus",
    "last_prices": "MarketDataService/GetLastPrices",
}
WRITE_METHODS = {"submit": "OrdersService/PostOrder", "cancel": "OrdersService/CancelOrder",
                 "submit_stop": "StopOrdersService/PostStopOrder",
                 "cancel_stop": "StopOrdersService/CancelStopOrder"}
SANDBOX_METHODS = {
    "accounts": "GetSandboxAccounts", "portfolio": "GetSandboxPortfolio",
    "positions": "GetSandboxPositions", "orders": "GetSandboxOrders",
    "withdraw_limits": "GetSandboxWithdrawLimits",
    "order": "GetSandboxOrderState", "max_lots": "GetSandboxMaxLots",
    "order_price": "GetSandboxOrderPrice", "stop_orders": "GetSandboxStopOrders",
    "submit": "PostSandboxOrder", "cancel": "CancelSandboxOrder",
    "submit_stop": "PostSandboxStopOrder", "cancel_stop": "CancelSandboxStopOrder",
}
STATUS = {"EXECUTION_REPORT_STATUS_NEW": "NEW",
          "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PARTIALLY_FILLED",
          "EXECUTION_REPORT_STATUS_FILL": "FILLED",
          "EXECUTION_REPORT_STATUS_CANCELLED": "CANCELLED",
          "EXECUTION_REPORT_STATUS_REJECTED": "REJECTED"}
FULL_ACCESS = "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
ACCESS_LEVELS = {FULL_ACCESS, "ACCOUNT_ACCESS_LEVEL_READ_ONLY",
                 "ACCOUNT_ACCESS_LEVEL_NO_ACCESS", "ACCOUNT_ACCESS_LEVEL_UNSPECIFIED"}
STOP_STATUSES = {"STOP_ORDER_STATUS_ACTIVE", "STOP_ORDER_STATUS_EXECUTED",
                 "STOP_ORDER_STATUS_CANCELED", "STOP_ORDER_STATUS_EXPIRED"}
INT64_MAX = 2**63 - 1
NANO = Decimal(10**9)


def _now():
    return datetime.now(timezone.utc).isoformat()


class TradingError(Exception):
    """Only a fixed local diagnostic, never a token, account or broker message."""
    def __init__(self, code, *, ambiguous=False, http_status=None, not_found=False):
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
            code = "ADAPTER_ERROR"
        super().__init__(code)
        self.code = code
        self.ambiguous = bool(ambiguous)
        self.http_status = http_status
        self.not_found = bool(not_found)


def _identifier(value, code="INVALID_IDENTIFIER"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise TradingError(code)
    return value


def _uuid(value, code="INVALID_CLIENT_ORDER_ID"):
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError
        parsed = UUID(value)
        if str(parsed) != value.lower() or parsed.int == 0:
            raise ValueError
        return str(parsed)
    except (ValueError, AttributeError, TypeError):
        raise TradingError(code) from None


def _integer(value, *, minimum=0, code="INVALID_INTEGER"):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and len(value) <= 20 and re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        result = int(value)
    else:
        raise TradingError(code)
    if result < minimum or result > INT64_MAX:
        raise TradingError(code)
    return result


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise TradingError("PRICE_REQUIRES_DECIMAL")
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise TradingError("INVALID_DECIMAL") from None


def decimal_to_quotation(value):
    """Exact nano conversion; floats and silent rounding are forbidden."""
    value = _decimal(value)
    if value.copy_abs() >= Decimal(2**63 + 1):
        raise TradingError("QUOTATION_OUT_OF_RANGE")
    with localcontext() as ctx:
        ctx.prec = 64
        units = int(value)
        nanos = (value - units) * NANO
        if units < -2**63 or units > INT64_MAX or nanos != nanos.to_integral_value():
            raise TradingError("QUOTATION_OUT_OF_RANGE")
        return {"units": str(units), "nano": int(nanos)}


def quotation_to_decimal(value):
    if not isinstance(value, dict):
        raise TradingError("INVALID_QUOTATION")
    units = _integer(value.get("units", "0"), minimum=-2**63, code="INVALID_QUOTATION")
    nano = _integer(value.get("nano", 0), minimum=-999999999, code="INVALID_QUOTATION")
    if nano > 999999999 or (units > 0 and nano < 0) or (units < 0 and nano > 0):
        raise TradingError("INVALID_QUOTATION")
    with localcontext() as ctx:
        ctx.prec = 64
        return Decimal(units) + Decimal(nano) / NANO


def _price(value):
    result = _decimal(value)
    if result <= 0:
        raise TradingError("PRICE_MUST_BE_POSITIVE")
    decimal_to_quotation(result)
    return result


def _side(value):
    if value not in ("BUY", "SELL"):
        raise TradingError("INVALID_SIDE")
    return value


def _timestamp(value):
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError
        return at.astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError, TypeError):
        raise TradingError("INVALID_TIMESTAMP") from None


def _money(value):
    if value is None:
        return None, None
    if not isinstance(value, dict) or not re.fullmatch(r"[A-Za-z]{3}", str(value.get("currency", ""))):
        raise TradingError("INVALID_MONEY")
    return quotation_to_decimal(value), value["currency"].upper()


def _serializable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    return value


@dataclass(frozen=True)
class ExecutionConfig:
    enabled: bool = False
    armed: bool = False
    allowed_account_ids: frozenset[str] = field(default_factory=frozenset, repr=False)
    allowed_instrument_uids: frozenset[str] = field(default_factory=frozenset)
    allow_stop_orders: bool = False
    allow_margin: bool = False
    environment: str = "production"

    def __post_init__(self):
        for name in ("enabled", "armed", "allow_stop_orders", "allow_margin"):
            if type(getattr(self, name)) is not bool:
                raise TradingError("INVALID_EXECUTION_CONFIG")
        if not isinstance(self.environment, str) or self.environment not in ROOTS:
            raise TradingError("INVALID_ENVIRONMENT")
        for name, checker in (("allowed_account_ids", _identifier), ("allowed_instrument_uids", _uuid)):
            value = getattr(self, name)
            if not isinstance(value, (set, frozenset, tuple, list)):
                raise TradingError("INVALID_EXECUTION_SCOPE")
            object.__setattr__(self, name, frozenset(checker(x) for x in value))


@dataclass(frozen=True)
class ExecutionFill:
    trade_id: str
    lots: int
    price: Decimal
    currency: str
    executed_at: str
    price_type: str = "POINT"


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str | None
    broker_order_id: str | None
    instrument_uid: str | None
    side: str | None
    lots_requested: int | None
    lots_executed: int | None
    status: str
    outcome: str
    broker_status: str | None = None
    average_fill_price: Decimal | None = None
    fill_price_currency: str | None = None
    requested_limit_price: Decimal | None = None
    executions: tuple[ExecutionFill, ...] = ()
    executed_commission: Decimal | None = None
    commission_currency: str | None = None
    code: str | None = None
    observed_at: str = field(default_factory=_now)
    # POINT only when stages came from an explicit GetOrderState POINT request.
    average_fill_price_type: str | None = None
    order_type: str | None = None
    limit_price: Decimal | None = None
    time_in_force: str | None = None

    @property
    def filled_lots(self):
        return self.lots_executed

    def to_dict(self):
        return _serializable(asdict(self))


@dataclass(frozen=True)
class StopOrderResult:
    client_order_id: str
    broker_order_id: str | None
    instrument_uid: str
    side: str
    lots_requested: int
    stop_price: Decimal
    limit_price: Decimal
    stop_type: str
    outcome: str
    status: str
    code: str | None = None
    observed_at: str = field(default_factory=_now)

    def to_dict(self):
        return _serializable(asdict(self))


@dataclass(frozen=True)
class CancelResult:
    broker_order_id: str | None
    client_order_id: str | None
    outcome: str
    status: str
    cancelled_at: str | None = None
    code: str | None = None

    def to_dict(self):
        return asdict(self)


class MemoryJournal:
    """Thread-safe secondary guard. Caller still needs a durable SENDING claim.

    Injectable journals implement get(key), reserve(key, fingerprint, intent),
    and finish(key, result). reserve atomically returns (created, record).
    A record contains fingerprint, intent and result (None while unresolved).
    It must survive crashes in a durable implementation; never erase UNKNOWN.
    """
    durable = False

    def __init__(self):
        self._lock = threading.RLock()
        self._records = {}

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self._records.get(key))

    def reserve(self, key, fingerprint, intent):
        with self._lock:
            if key in self._records:
                return False, copy.deepcopy(self._records[key])
            record = {"fingerprint": fingerprint, "intent": copy.deepcopy(intent), "result": None}
            self._records[key] = record
            return True, copy.deepcopy(record)

    def finish(self, key, result):
        with self._lock:
            self._records[key]["result"] = copy.deepcopy(result)


class HttpxTransport:
    """Pinned caller URLs, verified TLS, no redirects, proxy env or retries."""
    def __init__(self):
        self._client = None
        self._lock = threading.Lock()

    def __repr__(self):
        return "HttpxTransport(TBANK, NO_RETRIES)"

    def request(self, method, url, *, headers, json, timeout, follow_redirects=False):
        import httpx
        with self._lock:
            if self._client is None:
                context = ssl.create_default_context()
                root = Path(__file__).with_name("vendor") / "tbank/russian_trusted_root_ca.pem"
                if root.is_file():
                    context.load_verify_locations(cafile=str(root))
                self._client = httpx.Client(verify=context, trust_env=False, follow_redirects=False,
                                            transport=httpx.HTTPTransport(verify=context, retries=0, trust_env=False))
        return self._client.request(method, url, headers=headers, json=json,
                                    timeout=timeout, follow_redirects=False)

    def close(self):
        if self._client is not None:
            self._client.close()


class TBankTradingAdapter:
    def __init__(self, token, *, transport=None, config=None, journal=None, timeout=8):
        if not isinstance(token, str) or any(ch.isspace() for ch in token):
            raise TradingError("INVALID_TOKEN_ARGUMENT")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise TradingError("INVALID_TIMEOUT")
        self._token = token
        self.config = config if config is not None else ExecutionConfig()
        if not isinstance(self.config, ExecutionConfig):
            raise TradingError("INVALID_EXECUTION_CONFIG")
        self.transport = transport if transport is not None else HttpxTransport()
        self.journal = journal if journal is not None else MemoryJournal()
        self.timeout = timeout
        self._accounts = {}
        self._accounts_at = None
        self._lock = threading.RLock()

    def __repr__(self):
        return "TBankTradingAdapter(" + self.config.environment + ", " + self.capabilities()["mode"] + ")"

    @property
    def environment(self):
        return self.config.environment

    def capabilities(self, account_id=None):
        account = self._accounts.get(account_id, {}) if account_id is not None else {}
        fresh = self._accounts_at is not None and time.monotonic() - self._accounts_at <= 60
        access = account.get("accessLevel", "UNKNOWN") if fresh else "UNKNOWN"
        armed = self.config.enabled and self.config.armed
        return {"version": VERSION, "environment": self.config.environment,
                "mode": "EXPLICIT_TRADING" if armed else "READ_ONLY",
                "token_configured": bool(self._token), "token_access_level": access,
                "access_verified": fresh and bool(account),
                "module_supports_limit_orders": True, "module_supports_market_orders": False,
                "module_supports_stop_limit_orders": True,
                "execution_enabled": self.config.enabled, "execution_armed": self.config.armed,
                "orders_enabled_for_account": bool(armed and fresh and access == FULL_ACCESS
                    and account.get("status") == "ACCOUNT_STATUS_OPEN"
                    and account_id in self.config.allowed_account_ids
                    and self.config.allowed_instrument_uids),
                "journal_durable": bool(getattr(self.journal, "durable", False)),
                "caller_durable_claim_required": True, "automatic_mutation_retries": False}

    def _local_gate(self, account_id, instrument_uid=None, *, stop=False):
        if not self.config.enabled:
            raise TradingError("EXECUTION_DISABLED")
        if not self.config.armed:
            raise TradingError("EXECUTION_NOT_ARMED")
        if account_id not in self.config.allowed_account_ids:
            raise TradingError("ACCOUNT_OUTSIDE_EXECUTION_SCOPE")
        if instrument_uid is not None and instrument_uid not in self.config.allowed_instrument_uids:
            raise TradingError("INSTRUMENT_OUTSIDE_EXECUTION_SCOPE")
        if stop and not self.config.allow_stop_orders:
            raise TradingError("STOP_ORDERS_DISABLED")

    def _request(self, name, body):
        if name not in READ_METHODS and name not in WRITE_METHODS:
            raise TradingError("METHOD_NOT_ALLOWED")
        mutation = name in WRITE_METHODS
        if mutation:
            account_id = body.get("accountId")
            self._local_gate(account_id, body.get("instrumentId"), stop="stop" in name)
            known = self._accounts.get(account_id, {})
            if (self._accounts_at is None or time.monotonic() - self._accounts_at > 60
                    or known.get("accessLevel") != FULL_ACCESS or known.get("status") != "ACCOUNT_STATUS_OPEN"):
                raise TradingError("FULL_ACCESS_NOT_VERIFIED")
        if not self._token:
            raise TradingError("TOKEN_MISSING")
        route = (READ_METHODS | WRITE_METHODS)[name]
        if self.config.environment == "sandbox" and name in SANDBOX_METHODS:
            route = "SandboxService/" + SANDBOX_METHODS[name]
        url = ROOTS[self.config.environment] + PACKAGE + route
        try:
            response = self.transport.request("POST", url,
                headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json",
                         "x-app-name": "7992811.VERITAS"},
                json=copy.deepcopy(body), timeout=self.timeout, follow_redirects=False)
            status = response.status_code
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError
        except Exception:
            raise TradingError("TRANSPORT_ERROR", ambiguous=mutation) from None
        if 300 <= status < 400:
            raise TradingError("REDIRECT_REJECTED", ambiguous=mutation, http_status=status)
        try:
            payload = response.json()
        except Exception:
            raise TradingError("INVALID_BROKER_JSON", ambiguous=mutation, http_status=status) from None
        if not isinstance(payload, dict):
            raise TradingError("INVALID_BROKER_RESPONSE", ambiguous=mutation, http_status=status)
        if not 200 <= status < 300 or ("code" in payload and "message" in payload):
            missing = status == 404 or (status in (400, 404) and payload.get("code") in (5, "5", "NOT_FOUND"))
            raise TradingError("BROKER_NOT_FOUND" if missing else "BROKER_HTTP_" + str(status),
                ambiguous=mutation and (status in (408, 425) or status >= 500 or 200 <= status < 300),
                http_status=status, not_found=missing)
        return payload

    def get_accounts(self):
        reply = self._request("accounts", {"status": "ACCOUNT_STATUS_OPEN"})
        accounts = reply.get("accounts", [])
        if not isinstance(accounts, list):
            raise TradingError("INVALID_ACCOUNTS_RESPONSE")
        cache = {}
        for item in accounts:
            if not isinstance(item, dict):
                raise TradingError("INVALID_ACCOUNTS_RESPONSE")
            identifier = _identifier(item.get("id"), "INVALID_ACCOUNT_ID")
            if identifier in cache:
                raise TradingError("DUPLICATE_ACCOUNT_ID")
            entry = copy.deepcopy(item)
            if entry.get("accessLevel") not in ACCESS_LEVELS:
                entry["accessLevel"] = "ACCOUNT_ACCESS_LEVEL_UNSPECIFIED"
            cache[identifier] = entry
        self._accounts, self._accounts_at = cache, time.monotonic()
        return list(copy.deepcopy(cache).values())

    def get_future(self, instrument_uid):
        uid = _uuid(instrument_uid, "INVALID_INSTRUMENT_UID")
        result = self._request("future", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}).get("instrument")
        if not isinstance(result, dict) or result.get("uid") != uid:
            raise TradingError("FUTURE_IDENTITY_MISMATCH")
        if result.get("realExchange") != "REAL_EXCHANGE_MOEX":
            raise TradingError("FUTURE_VENUE_MISMATCH")
        _integer(result.get("lot"), minimum=1, code="INVALID_FUTURE_LOT")
        if quotation_to_decimal(result.get("minPriceIncrement")) <= 0:
            raise TradingError("INVALID_FUTURE_TICK")
        if quotation_to_decimal(result.get("minPriceIncrementAmount")) <= 0:
            raise TradingError("INVALID_FUTURE_TICK_AMOUNT")
        _identifier(result.get("ticker"), "INVALID_FUTURE_TICKER")
        return copy.deepcopy(result)

    def get_portfolio(self, account_id):
        account = _identifier(account_id, "INVALID_ACCOUNT_ID")
        result = self._request("portfolio", {"accountId": account, "currency": "RUB"})
        if result.get("accountId") != account or not isinstance(result.get("positions", []), list):
            raise TradingError("PORTFOLIO_IDENTITY_MISMATCH")
        return result

    def get_positions(self, account_id):
        account = _identifier(account_id, "INVALID_ACCOUNT_ID")
        result = self._request("positions", {"accountId": account})
        # Proto account_id is optional; the authenticated request scopes legacy responses.
        if "accountId" in result and result["accountId"] != account:
            raise TradingError("POSITIONS_IDENTITY_MISMATCH")
        for key in ("futures", "securities", "money", "blocked"):
            if not isinstance(result.get(key, []), list):
                raise TradingError("INVALID_POSITIONS_RESPONSE")
        return result

    def get_withdraw_limits(self, account_id):
        """Read reported cash/blocked/futures-guarantee buckets, never withdraw.

        WithdrawLimitsResponse contains repeated MoneyValue arrays: money,
        blocked, blockedGuarantee. Unlike GetPositions, it exposes funds tied
        up as futures guarantee collateral. It has no required accountId echo;
        the authenticated request provides its account scope. No allocation
        or free-margin amount is fabricated from missing cash here.
        """
        account = _identifier(account_id, "INVALID_ACCOUNT_ID")
        result = self._request("withdraw_limits", {"accountId": account})
        if "accountId" in result and result["accountId"] != account:
            raise TradingError("WITHDRAW_LIMITS_IDENTITY_MISMATCH")
        for key in ("money", "blocked", "blockedGuarantee"):
            rows = result.get(key, [])
            if not isinstance(rows, list):
                raise TradingError("INVALID_WITHDRAW_LIMITS_RESPONSE")
            currencies = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise TradingError("INVALID_WITHDRAW_LIMITS_RESPONSE")
                amount, currency = _money(row)
                if (currency in currencies
                        or (key != "money" and amount < 0)):
                    raise TradingError("INVALID_WITHDRAW_LIMITS_RESPONSE")
                currencies.add(currency)
        return result

    def get_order_book(self, instrument_uid, depth=1):
        uid = _uuid(instrument_uid, "INVALID_INSTRUMENT_UID")
        if type(depth) is not int or depth not in (1, 10, 20, 30, 40, 50):
            raise TradingError("INVALID_ORDER_BOOK_DEPTH")
        result = self._request("book", {"instrumentId": uid, "depth": depth})
        if result.get("instrumentUid") != uid:
            raise TradingError("ORDER_BOOK_IDENTITY_MISMATCH")
        return result

    def get_trading_status(self, instrument_uid):
        uid = _uuid(instrument_uid, "INVALID_INSTRUMENT_UID")
        result = self._request("trading_status", {"instrumentId": uid})
        if result.get("instrumentUid") != uid:
            raise TradingError("TRADING_STATUS_IDENTITY_MISMATCH")
        return result

    def get_last_prices(self, instrument_uids):
        if not isinstance(instrument_uids, (tuple, list)) or not 1 <= len(instrument_uids) <= 100:
            raise TradingError("INVALID_INSTRUMENT_LIST")
        uids = [_uuid(x, "INVALID_INSTRUMENT_UID") for x in instrument_uids]
        result = self._request("last_prices", {"instrumentId": uids})
        prices = result.get("lastPrices", [])
        if not isinstance(prices, list) or any(not isinstance(q, dict) or q.get("instrumentUid") not in uids for q in prices):
            raise TradingError("LAST_PRICES_IDENTITY_MISMATCH")
        return result

    def get_max_lots(self, account_id, instrument_uid, price=None):
        body = {"accountId": _identifier(account_id, "INVALID_ACCOUNT_ID"),
                "instrumentId": _uuid(instrument_uid, "INVALID_INSTRUMENT_UID")}
        if price is not None:
            body["price"] = decimal_to_quotation(_price(price))
        return self._request("max_lots", body)

    def get_order_price(self, account_id, instrument_uid, side, lots, price):
        body = self._intent(account_id, instrument_uid, side, lots, price, None)
        return self._request("order_price", {k: body[k] for k in ("accountId", "instrumentId", "direction", "quantity", "price")})

    def _intent(self, account_id, uid, side, lots, price, client_order_id, time_in_force="DAY"):
        if type(lots) is not int:
            raise TradingError("LOTS_MUST_BE_INTEGER")
        if not isinstance(time_in_force, str):
            raise TradingError("INVALID_FUTURES_TIME_IN_FORCE")
        tif = {"DAY": "TIME_IN_FORCE_DAY", "FILL_AND_KILL": "TIME_IN_FORCE_FILL_AND_KILL",
               "TIME_IN_FORCE_DAY": "TIME_IN_FORCE_DAY", "TIME_IN_FORCE_FILL_AND_KILL": "TIME_IN_FORCE_FILL_AND_KILL"}.get(time_in_force)
        if tif is None:
            raise TradingError("INVALID_FUTURES_TIME_IN_FORCE")
        body = {"accountId": _identifier(account_id, "INVALID_ACCOUNT_ID"),
                "instrumentId": _uuid(uid, "INVALID_INSTRUMENT_UID"),
                "direction": "ORDER_DIRECTION_" + _side(side),
                "quantity": str(_integer(lots, minimum=1, code="INVALID_LOTS")),
                "price": decimal_to_quotation(_price(price)), "orderType": "ORDER_TYPE_LIMIT",
                "timeInForce": tif, "priceType": "PRICE_TYPE_POINT",
                "confirmMarginTrade": self.config.allow_margin}
        if client_order_id is not None:
            body["orderId"] = _uuid(client_order_id)
        return body

    def _prepare_write(self, body, *, stop=False):
        account, uid = body["accountId"], body.get("instrumentId")
        self._local_gate(account, uid, stop=stop)
        self.get_accounts()
        info = self._accounts.get(account, {})
        if info.get("accessLevel") != FULL_ACCESS or info.get("status") != "ACCOUNT_STATUS_OPEN":
            raise TradingError("FULL_ACCESS_NOT_VERIFIED")
        if uid is not None:
            spec = self.get_future(uid)
            if spec.get("apiTradeAvailableFlag") is not True:
                raise TradingError("INSTRUMENT_API_TRADE_UNAVAILABLE")
            side = body["direction"].rsplit("_", 1)[-1]
            if spec.get("buyAvailableFlag" if side == "BUY" else "sellAvailableFlag") is not True:
                raise TradingError("INSTRUMENT_SIDE_UNAVAILABLE")
            tick = quotation_to_decimal(spec["minPriceIncrement"])
            for key in ("price", "stopPrice"):
                if key in body:
                    with localcontext() as ctx:
                        ctx.prec = 64
                        if quotation_to_decimal(body[key]) % tick:
                            raise TradingError("PRICE_NOT_ON_TICK")

    def _normalize_order(self, raw, *, expected=None, points=False):
        if not isinstance(raw, dict):
            raise TradingError("INVALID_ORDER_RESPONSE")
        order_id = _identifier(raw.get("orderId"), "INVALID_BROKER_ORDER_ID")
        request_id = _uuid(raw["orderRequestId"]) if raw.get("orderRequestId") else None
        uid = _uuid(raw.get("instrumentUid"), "INVALID_ORDER_INSTRUMENT")
        side = {"ORDER_DIRECTION_BUY": "BUY", "ORDER_DIRECTION_SELL": "SELL"}.get(raw.get("direction"))
        if side is None or raw.get("executionReportStatus") not in STATUS:
            raise TradingError("INVALID_ORDER_STATUS_OR_SIDE")
        status = STATUS[raw["executionReportStatus"]]
        requested = _integer(raw.get("lotsRequested"), minimum=1, code="INVALID_ORDER_LOTS")
        executed = _integer(raw.get("lotsExecuted"), code="INVALID_ORDER_FILLED_LOTS")
        if (executed > requested or (status == "FILLED" and executed != requested)
                or (status == "PARTIALLY_FILLED" and not 0 < executed < requested)
                or (status in ("NEW", "REJECTED") and executed != 0)):
            raise TradingError("INCONSISTENT_ORDER_FILLED_LOTS")
        if expected is not None:
            if (request_id != expected["orderId"] or uid != expected["instrumentId"]
                    or raw["direction"] != expected["direction"] or requested != int(expected["quantity"])
                    or raw.get("orderType") != "ORDER_TYPE_LIMIT"):
                raise TradingError("ORDER_RESPONSE_INTENT_MISMATCH")
        order_type = raw.get("orderType")
        if order_type not in ("ORDER_TYPE_LIMIT", "ORDER_TYPE_MARKET", "ORDER_TYPE_BESTPRICE"):
            raise TradingError("INVALID_ORDER_TYPE")
        limit_price = None
        if points and order_type == "ORDER_TYPE_LIMIT" and raw.get("initialSecurityPrice") is not None:
            limit_price, _ = _money(raw["initialSecurityPrice"])
            if limit_price is None or limit_price <= 0:
                raise TradingError("INVALID_ORDER_LIMIT_PRICE")
        fills = {}
        if points:
            stages = raw.get("stages", [])
            if not isinstance(stages, list):
                raise TradingError("INVALID_EXECUTION_STAGES")
            for stage in stages:
                if not isinstance(stage, dict):
                    raise TradingError("INVALID_EXECUTION_STAGE")
                tid = _identifier(stage.get("tradeId"), "INVALID_TRADE_ID")
                value, currency = _money(stage.get("price"))
                if value is None or value <= 0:
                    raise TradingError("INVALID_EXECUTION_PRICE")
                fill = ExecutionFill(tid, _integer(stage.get("quantity"), minimum=1, code="INVALID_EXECUTION_LOTS"),
                                     value, currency, _timestamp(stage.get("executionTime")))
                if tid in fills and fills[tid] != fill:
                    raise TradingError("CONFLICTING_EXECUTION_ID")
                fills[tid] = fill
        total = sum(f.lots for f in fills.values())
        if total > executed:
            raise TradingError("EXECUTIONS_EXCEED_FILLED_LOTS")
        average, currency = None, None
        if total and total == executed:
            currencies = {f.currency for f in fills.values()}
            if len(currencies) != 1:
                raise TradingError("EXECUTION_CURRENCY_MISMATCH")
            with localcontext() as ctx:
                ctx.prec = 64
                average = sum(f.price * f.lots for f in fills.values()) / total
            currency = next(iter(currencies))
        # executedOrderPrice has different semantics in PostOrder and OrderState.
        # Never treat that broker aggregate as an average or manufacture fills.
        commission, commission_currency = _money(raw.get("executedCommission"))
        if commission is not None and commission < 0:
            raise TradingError("INVALID_EXECUTED_COMMISSION")
        return OrderResult(request_id, order_id, uid, side, requested, executed, status,
            "REJECTED" if status == "REJECTED" else "ACCEPTED", raw["executionReportStatus"],
            average, currency, quotation_to_decimal(expected["price"]) if expected else None,
            tuple(fills.values()), commission, commission_currency,
            average_fill_price_type="POINT" if average is not None else None,
            order_type=order_type, limit_price=limit_price, time_in_force=raw.get("timeInForce"))

    def get_order(self, account_id, order_id=None, *, client_order_id=None):
        if (order_id is None) == (client_order_id is None):
            raise TradingError("EXACTLY_ONE_ORDER_ID_REQUIRED")
        account = _identifier(account_id, "INVALID_ACCOUNT_ID")
        identifier = _uuid(client_order_id) if client_order_id is not None else _identifier(order_id, "INVALID_BROKER_ORDER_ID")
        body = {"accountId": account, "orderId": identifier, "priceType": "PRICE_TYPE_POINT",
                "orderIdType": "ORDER_ID_TYPE_REQUEST" if client_order_id is not None else "ORDER_ID_TYPE_EXCHANGE"}
        result = self._normalize_order(self._request("order", body), points=True)
        if (client_order_id is not None and result.client_order_id != identifier
                or order_id is not None and result.broker_order_id != identifier):
            raise TradingError("ORDER_LOOKUP_IDENTITY_MISMATCH")
        return result

    def get_order_executions(self, account_id, order_id=None, *, client_order_id=None):
        return self.get_order(account_id, order_id, client_order_id=client_order_id).executions

    def list_orders(self, account_id):
        reply = self._request("orders", {"accountId": _identifier(account_id, "INVALID_ACCOUNT_ID")})
        rows = reply.get("orders", [])
        if not isinstance(rows, list):
            raise TradingError("INVALID_ORDERS_RESPONSE")
        # GetOrders has no priceType parameter: stages here cannot be labelled POINT.
        result = [self._normalize_order(raw) for raw in rows]
        if len({x.broker_order_id for x in result}) != len(result):
            raise TradingError("DUPLICATE_BROKER_ORDER_ID")
        return result

    def _key(self, account, client):
        return (self.config.environment, account, client)

    def _fingerprint(self, kind, body):
        return hashlib.sha256(json.dumps({"kind": kind, "body": body}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _journal(self, method, *args):
        try:
            return getattr(self.journal, method)(*args)
        except Exception:
            # A persistence failure after submission is uncertain, never a reason
            # for a second POST. The caller's durable SENDING state remains held.
            raise TradingError("JOURNAL_ERROR", ambiguous=method == "finish") from None

    def _unknown(self, body, code, *, rejected=False):
        return OrderResult(body.get("orderId"), None, body.get("instrumentId"),
            body.get("direction", "").rsplit("_", 1)[-1] or None,
            int(body["quantity"]) if "quantity" in body else None,
            0 if rejected else None, "REJECTED" if rejected else "UNKNOWN",
            "REJECTED" if rejected else "UNKNOWN", requested_limit_price=quotation_to_decimal(body["price"]) if "price" in body else None,
            code=code)

    def _matching_order(self, result, body):
        if (result.client_order_id != body["orderId"] or result.instrument_uid != body["instrumentId"]
                or result.side != body["direction"].rsplit("_", 1)[-1]
                or result.lots_requested != int(body["quantity"])
                or result.order_type != "ORDER_TYPE_LIMIT"
                or result.limit_price != quotation_to_decimal(body["price"])
                or result.time_in_force is not None and result.time_in_force != body["timeInForce"]):
            raise TradingError("RECONCILIATION_INTENT_MISMATCH")
        return result

    def reconcile_submission(self, account_id, client_order_id):
        """Read only. A missing order never authorizes resubmission."""
        account, client = _identifier(account_id, "INVALID_ACCOUNT_ID"), _uuid(client_order_id)
        key = self._key(account, client)
        record = self._journal("get", key)
        body = record["intent"] if record else {"accountId": account, "orderId": client}
        if record and body.get("stopOrderType"):
            return record.get("result") or self._unknown(body, "STOP_REQUIRES_MANUAL_RECONCILIATION")
        try:
            result = self.get_order(account, client_order_id=client)
            if record:
                self._matching_order(result, body)
                self._journal("finish", key, result)
            return result
        except TradingError as exc:
            return self._unknown(body, "ORDER_NOT_FOUND_UNRESOLVED" if exc.not_found else exc.code)

    def submit_limit(self, account_id, instrument_uid, side, lots, limit_price, client_order_id, *, time_in_force="DAY"):
        """Exactly one PostOrder after durable caller claim; no market fallback."""
        body = self._intent(account_id, instrument_uid, side, lots, limit_price, _uuid(client_order_id), time_in_force)
        key, fingerprint = self._key(body["accountId"], body["orderId"]), self._fingerprint("LIMIT", body)
        with self._lock:
            self._local_gate(body["accountId"], body["instrumentId"])
            old = self._journal("get", key)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                if old.get("result") is not None and old["result"].outcome != "UNKNOWN":
                    return old["result"]
                return self.reconcile_submission(body["accountId"], body["orderId"])
            self._prepare_write(body)
            # Recover a completed/active order after worker restart by request UUID.
            try:
                existing = self.get_order(body["accountId"], client_order_id=body["orderId"])
            except TradingError as exc:
                if not exc.not_found:
                    raise
            else:
                existing = self._matching_order(existing, body)
                created, record = self._journal("reserve", key, fingerprint, body)
                if not created and record["fingerprint"] != fingerprint:
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                self._journal("finish", key, existing)
                return existing
            created, record = self._journal("reserve", key, fingerprint, body)
            if not created:
                if record["fingerprint"] != fingerprint:
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                return record.get("result") or self._unknown(body, "SUBMISSION_IN_PROGRESS")
            try:
                raw = self._request("submit", body)
                try:
                    result = self._normalize_order(raw, expected=body)
                except TradingError as exc:
                    result = self._unknown(body, exc.code)
            except TradingError as exc:
                result = self._unknown(body, exc.code, rejected=not exc.ambiguous)
            self._journal("finish", key, result)
            return result

    def list_stop_orders(self, account_id, *, status="ACTIVE"):
        if status not in ("ALL", "ACTIVE", "EXECUTED", "CANCELED", "EXPIRED"):
            raise TradingError("INVALID_STOP_STATUS")
        reply = self._request("stop_orders", {"accountId": _identifier(account_id, "INVALID_ACCOUNT_ID"),
                                              "status": "STOP_ORDER_STATUS_" + status})
        rows = reply.get("stopOrders", [])
        if not isinstance(rows, list):
            raise TradingError("INVALID_STOP_ORDERS_RESPONSE")
        ids = set()
        for row in rows:
            if not isinstance(row, dict):
                raise TradingError("INVALID_STOP_ORDER_RESPONSE")
            identifier = _identifier(row.get("stopOrderId"), "INVALID_STOP_ORDER_ID")
            _uuid(row.get("instrumentUid"), "INVALID_INSTRUMENT_UID")
            _integer(row.get("lotsRequested"), minimum=1, code="INVALID_STOP_LOTS")
            if row.get("status") not in STOP_STATUSES or identifier in ids:
                raise TradingError("INVALID_STOP_STATUS_OR_DUPLICATE")
            ids.add(identifier)
        return rows

    def submit_stop_limit(self, account_id, instrument_uid, side, lots, stop_price, limit_price,
                          client_order_id, *, stop_type="STOP_LIMIT", expires_at=None):
        if stop_type not in ("STOP_LIMIT", "TAKE_PROFIT"):
            raise TradingError("ONLY_LIMIT_CHILD_STOPS_SUPPORTED")
        body = self._intent(account_id, instrument_uid, side, lots, limit_price, _uuid(client_order_id))
        body.pop("orderType")
        body.pop("timeInForce")
        body.update(direction="STOP_ORDER_DIRECTION_" + side, stopPrice=decimal_to_quotation(_price(stop_price)),
                    stopOrderType="STOP_ORDER_TYPE_" + stop_type,
                    exchangeOrderType="EXCHANGE_ORDER_TYPE_LIMIT",
                    expirationType="STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL")
        if stop_type == "TAKE_PROFIT":
            body["takeProfitType"] = "TAKE_PROFIT_TYPE_REGULAR"
        if expires_at is not None:
            expiry = _timestamp(expires_at)
            if datetime.fromisoformat(expiry) <= datetime.now(timezone.utc):
                raise TradingError("STOP_EXPIRY_NOT_FUTURE")
            body.update(expireDate=expiry, expirationType="STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_DATE")
        key, fingerprint = self._key(body["accountId"], body["orderId"]), self._fingerprint("STOP", body)
        def receipt(outcome, broker=None, code=None):
            return StopOrderResult(body["orderId"], broker, body["instrumentId"], side, lots,
                                   _price(stop_price), _price(limit_price), stop_type, outcome,
                                   "ACKNOWLEDGED" if outcome == "ACCEPTED" else outcome, code)
        with self._lock:
            self._local_gate(body["accountId"], body["instrumentId"], stop=True)
            old = self._journal("get", key)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                return old.get("result") or receipt("UNKNOWN", code="STOP_REQUIRES_MANUAL_RECONCILIATION")
            self._prepare_write(body, stop=True)
            created, record = self._journal("reserve", key, fingerprint, body)
            if not created:
                if record["fingerprint"] != fingerprint:
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                return record.get("result") or receipt("UNKNOWN", code="SUBMISSION_IN_PROGRESS")
            try:
                raw = self._request("submit_stop", body)
                try:
                    if _uuid(raw.get("orderRequestId")) != body["orderId"]:
                        raise TradingError("STOP_RESPONSE_INTENT_MISMATCH")
                    result = receipt("ACCEPTED", _identifier(raw.get("stopOrderId"), "INVALID_STOP_ORDER_ID"))
                except TradingError as exc:
                    result = receipt("UNKNOWN", code=exc.code)
            except TradingError as exc:
                result = receipt("UNKNOWN" if exc.ambiguous else "REJECTED", code=exc.code)
            self._journal("finish", key, result)
            return result

    def _cancel(self, name, body, broker_id, client_id):
        try:
            raw = self._request(name, body)
            try:
                at = _timestamp(raw.get("time"))
            except TradingError as exc:
                return CancelResult(broker_id, client_id, "UNKNOWN", "UNKNOWN", code=exc.code)
            return CancelResult(broker_id, client_id, "ACCEPTED", "CANCEL_REQUESTED", at)
        except TradingError as exc:
            state = "UNKNOWN" if exc.ambiguous else "REJECTED"
            return CancelResult(broker_id, client_id, state, state, code=exc.code)

    def cancel_order(self, account_id, order_id=None, *, client_order_id=None):
        account = _identifier(account_id, "INVALID_ACCOUNT_ID")
        self._local_gate(account)
        current = self.get_order(account, order_id, client_order_id=client_order_id)
        self._local_gate(account, current.instrument_uid)
        self._prepare_write({"accountId": account})
        return self._cancel("cancel", {"accountId": account, "orderId": current.broker_order_id,
                                       "orderIdType": "ORDER_ID_TYPE_EXCHANGE"}, current.broker_order_id, current.client_order_id)

    def cancel_stop_order(self, account_id, stop_order_id):
        account, identifier = _identifier(account_id, "INVALID_ACCOUNT_ID"), _identifier(stop_order_id, "INVALID_STOP_ORDER_ID")
        self._local_gate(account, stop=True)
        found = [x for x in self.list_stop_orders(account, status="ALL") if x["stopOrderId"] == identifier]
        if len(found) != 1:
            raise TradingError("STOP_ORDER_NOT_FOUND", not_found=True)
        self._local_gate(account, found[0]["instrumentUid"], stop=True)
        self._prepare_write({"accountId": account}, stop=True)
        return self._cancel("cancel_stop", {"accountId": account, "stopOrderId": identifier}, identifier, None)

    def close(self):
        close = getattr(self.transport, "close", None)
        if close is not None:
            close()
