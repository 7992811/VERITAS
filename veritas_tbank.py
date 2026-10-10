"""T-Invest gRPC connection, strictly read-only and separate from paper trading."""
from __future__ import annotations

import copy
import hmac
import html
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import veritas_index_future_proxy as VIFP

VERSION = "R87_TBANK_THREE_FUTURES_ALL_TIMEFRAMES"
TARGET = "invest-public-api.tbank.ru:443"
PACKAGE = "tinkoff.public.invest.api.contract.v1"
TOKEN_ENV = "TBANK_API_TOKEN"
RPC_TIMEOUT = 8
STREAM_PING_MS = 5000
STREAM_HEARTBEAT_MAX_AGE_SECONDS = 20
STREAM_MARKET_RECOVERY_AGE_SECONDS = 30
STREAM_RECOVERY_COOLDOWN_SECONDS = 10
STREAM_RECONNECT_MAX_SECONDS = 5
DEFAULT_TICKERS = {"CNYRUBF": "CNYRUBF", "NQF": "NAZ6", "MOEXF": "IMOEXF"}
LABELS = {"CNYRUBF": "CNYRUBf", "NQF": "NQf", "MOEXF": "MOEXf"}
TIMEFRAMES = ("1m", "5m", "1h", "4h", "1d", "3d", "7d")
# Native history: enum, lookback days, refresh seconds, response cap.
HISTORY = {
    "1m": ("CANDLE_INTERVAL_1_MIN", 1, 20, 2400),
    "5m": ("CANDLE_INTERVAL_5_MIN", 1, 60, 2400),
    "1h": ("CANDLE_INTERVAL_HOUR", 21, 300, 2400),
    "4h": ("CANDLE_INTERVAL_4_HOUR", 30, 900, 700),
    "1d": ("CANDLE_INTERVAL_DAY", 365, 3600, 2400),
    "7d": ("CANDLE_INTERVAL_WEEK", 2 * 365, 3600, 300),
}
METHODS = {
    "accounts": ("UsersService/GetAccounts", "GetAccountsRequest", "GetAccountsResponse"),
    "find": ("InstrumentsService/FindInstrument", "FindInstrumentRequest", "FindInstrumentResponse"),
    "instrument": ("InstrumentsService/GetInstrumentBy", "InstrumentRequest", "InstrumentResponse"),
    "future": ("InstrumentsService/FutureBy", "InstrumentRequest", "FutureResponse"),
    "prices": ("MarketDataService/GetLastPrices", "GetLastPricesRequest", "GetLastPricesResponse"),
    "candles": ("MarketDataService/GetCandles", "GetCandlesRequest", "GetCandlesResponse"),
    "book": ("MarketDataService/GetOrderBook", "GetOrderBookRequest", "GetOrderBookResponse"),
    "trading_status": ("MarketDataService/GetTradingStatus", "GetTradingStatusRequest", "GetTradingStatusResponse"),
    "portfolio": ("OperationsService/GetPortfolio", "PortfolioRequest", "PortfolioResponse"),
    "positions": ("OperationsService/GetPositions", "PositionsRequest", "PositionsResponse"),
}


def utcnow():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or utcnow()).isoformat().replace("+00:00", "Z")


def price(value):
    return float(Decimal(str(value.get("units", 0))) + Decimal(str(value.get("nano", 0))) / Decimal(10**9))


def age_seconds(timestamp, now=None):
    try:
        at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = ((now or utcnow()) - at).total_seconds()
        return round(age, 3) if age >= -5 else None
    except (AttributeError, TypeError, ValueError):
        return None


def private_access(headers, environ=None):
    env = os.environ if environ is None else environ
    expected = env.get("VERITAS_APP_AUTH_TOKEN", "").strip() or env.get("VERITAS_AUTOMATION_TOKEN", "").strip()
    supplied = headers.get("X-Veritas-Token", "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


class TBankError(Exception):
    """Only a fixed diagnostic code, never RPC details or authentication metadata."""


class GrpcReader:
    def __init__(self, token, channel=None):
        import grpc
        from google.protobuf.json_format import MessageToDict, ParseDict
        from vendor.tbank import readonly_pb2

        if not token:
            raise TBankError("TOKEN_MISSING")
        self.grpc, self.proto = grpc, readonly_pb2
        self.to_dict, self.parse = MessageToDict, ParseDict
        self._metadata = (("authorization", "Bearer " + token), ("x-app-name", "7992811.VERITAS"))
        if channel is None:
            import ssl
            roots = b"".join(ssl.DER_cert_to_PEM_cert(cert).encode() for cert in ssl.create_default_context().get_ca_certs(binary_form=True))
            roots += Path(__file__).with_name("vendor").joinpath("tbank/russian_trusted_root_ca.pem").read_bytes()
            channel = grpc.secure_channel(TARGET, grpc.ssl_channel_credentials(roots), options=(
                ("grpc.max_receive_message_length", 4 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 60000),
                ("grpc.keepalive_timeout_ms", 10000),
            ))
        self._channel = channel

    def __repr__(self):
        return "GrpcReader(TBANK, READ_ONLY)"

    def call(self, name, **fields):
        if name not in METHODS:
            raise TBankError("METHOD_NOT_ALLOWED")
        method, request, response = METHODS[name]
        req = self.parse(fields, getattr(self.proto, request)())
        rpc = self._channel.unary_unary(
            "/" + PACKAGE + "." + method,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=getattr(self.proto, response).FromString,
        )
        try:
            reply = rpc(req, metadata=self._metadata, timeout=RPC_TIMEOUT)
            return self.to_dict(reply, preserving_proto_field_name=True)
        except self.grpc.RpcError as exc:
            raise TBankError(exc.code().name) from None

    def stream_market(self, ids, *, depth=10, include_info=True,
                      ping_ms=STREAM_PING_MS):
        if (not isinstance(ids, (list, tuple)) or not ids
                or any(not isinstance(uid, str) or not uid for uid in ids)):
            raise TBankError("INVALID_STREAM_INSTRUMENTS")
        if depth is not None and depth not in (1, 10, 20, 30, 40, 50):
            raise TBankError("INVALID_STREAM_BOOK_DEPTH")
        if type(ping_ms) is not int or not 5000 <= ping_ms <= 180000:
            raise TBankError("INVALID_STREAM_PING")
        fields = {
            "subscribe_last_price_request": {
                "subscription_action": "SUBSCRIPTION_ACTION_SUBSCRIBE",
                "instruments": [{"instrument_id": uid} for uid in ids],
            },
            "ping_settings": {"ping_delay_ms": ping_ms},
        }
        if depth is not None:
            fields["subscribe_order_book_request"] = {
                "subscription_action": "SUBSCRIPTION_ACTION_SUBSCRIBE",
                "instruments": [{
                    "instrument_id": uid, "depth": depth,
                    "order_book_type": "ORDERBOOK_TYPE_EXCHANGE",
                } for uid in ids],
            }
        if include_info:
            fields["subscribe_info_request"] = {
                "subscription_action": "SUBSCRIPTION_ACTION_SUBSCRIBE",
                "instruments": [{"instrument_id": uid} for uid in ids],
            }
        req = self.parse(fields, self.proto.MarketDataServerSideStreamRequest())
        rpc = self._channel.unary_stream(
            "/" + PACKAGE + ".MarketDataStreamService/MarketDataServerSideStream",
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=self.proto.MarketDataResponse.FromString,
        )
        try:
            for item in rpc(req, metadata=self._metadata, timeout=300):
                yield self.to_dict(item, preserving_proto_field_name=True)
        except self.grpc.RpcError as exc:
            raise TBankError(exc.code().name) from None

    def stream_prices(self, ids):
        # Compatibility wrapper for callers/tests that only need last prices.
        yield from self.stream_market(ids, depth=None, include_info=False)

    def close(self):
        self._channel.close()


def exact_instrument(reader, ticker):
    """Never substitute another expiry, exchange, or a similarly named ticker."""
    candidates = reader.call("find", query=ticker).get("instruments", [])
    matches = {item["uid"]: item for item in candidates
               if item.get("uid") and item.get("ticker", "").upper() == ticker.upper()}
    if len(matches) != 1:
        raise TBankError("INSTRUMENT_NOT_FOUND" if not matches else "INSTRUMENT_AMBIGUOUS")
    uid = next(iter(matches))
    result = reader.call("instrument", id_type="INSTRUMENT_ID_TYPE_UID", id=uid).get("instrument", {})
    if result.get("uid") != uid or result.get("ticker", "").upper() != ticker.upper():
        raise TBankError("INSTRUMENT_IDENTITY_MISMATCH")
    return result



def resolve_index_execution_future(reader, preferred_ticker="IMOEXF"):
    """Resolve MOEX signal execution to perpetual, else nearest expiry >1 day."""
    candidates=[]
    errors=[]
    try:
        preferred=future_metadata(reader, exact_instrument(reader, preferred_ticker))
        candidates.append(preferred)
    except TBankError as exc:
        errors.append(str(exc))

    # Fallback discovery is deliberately narrow and broker-native. Each
    # candidate still passes exact UID metadata, MOEX venue and futures spec
    # validation before the generic selector sees it.
    if not candidates:
        seen=set()
        for query in ("IMOEX", "Индекс МосБиржи", "MOEX"):
            try:
                found=reader.call("find", query=query).get("instruments", [])
            except Exception:
                continue
            for item in found:
                uid=item.get("uid")
                ticker=item.get("ticker")
                if not uid or not ticker or uid in seen:
                    continue
                seen.add(uid)
                try:
                    meta=future_metadata(reader, {"uid":uid,"ticker":ticker})
                except (TBankError,KeyError,TypeError,ValueError) as exc:
                    errors.append(str(exc))
                    continue
                basic=str(meta.get("basic_asset") or meta.get("basic_asset_position_uid") or "").upper()
                name=str(meta.get("name") or "").upper()
                tick=str(meta.get("ticker") or "").upper()
                if not ("IMOEX" in basic or "MOEX" in basic or "МОСБИРЖ" in name
                        or tick=="IMOEXF"):
                    continue
                candidates.append(meta)

    selected=VIFP.choose(candidates, now=utcnow())
    if not selected.get("eligible"):
        raise TBankError("INDEX_FUTURE_EXECUTION_PROXY_UNAVAILABLE")
    instrument=dict(selected["instrument"])
    instrument.update(execution_proxy_version=VIFP.VERSION,
                      execution_selection=selected["selection"],
                      signal_asset="MOEX",execution_asset="MOEXF",
                      minimum_expiry_buffer_seconds=selected["minimum_expiry_buffer_seconds"])
    return instrument


def normalized_quote(item, received_at=None):
    value = price(item.get("price", {}))
    if value <= 0 or not item.get("instrument_uid") or not item.get("time"):
        raise TBankError("INVALID_QUOTE")
    return {"instrument_uid": item["instrument_uid"], "price": value,
            "observed_at": item["time"], "received_at": received_at or iso(),
            "source": "TBANK_GRPC", "price_unit": "BROKER_NATIVE"}


def future_metadata(reader, instrument):
    uid, ticker = instrument["uid"], instrument["ticker"]
    result = reader.call("future", id_type="INSTRUMENT_ID_TYPE_UID", id=uid).get("instrument", {})
    if result.get("uid") != uid or result.get("ticker", "").upper() != ticker.upper():
        raise TBankError("FUTURE_IDENTITY_MISMATCH")
    if result.get("real_exchange") != "REAL_EXCHANGE_MOEX":
        raise TBankError("FUTURE_VENUE_MISMATCH")
    if price(result.get("min_price_increment", {})) <= 0 or not result.get("lot"):
        raise TBankError("FUTURE_SPECIFICATION_INCOMPLETE")
    expiry = result.get("expiration_date")
    perpetual = ticker.upper() in ("CNYRUBF", "IMOEXF")
    if not perpetual:
        try:
            valid_expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")) > utcnow()
        except (AttributeError, TypeError, ValueError):
            valid_expiry = False
        if not valid_expiry:
            raise TBankError("FUTURE_EXPIRED_OR_EXPIRY_MISSING")
    return {**instrument, **result, "perpetual": perpetual, "spec_observed_at": iso()}


def closed_candles(items, now=None):
    """Reject invalid or future observations; never manufacture missing bars."""
    now = now or utcnow()
    bars = {}
    for item in items:
        if not item.get("is_complete") or item.get("candle_source") == "CANDLE_SOURCE_DEALER_WEEKEND":
            continue
        try:
            at = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
            if at > now or at.tzinfo is None:
                continue
            bar = {key: price(item.get(key, {})) for key in ("open", "high", "low", "close")}
            if min(bar.values()) <= 0 or bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]):
                continue
            bar.update(time=iso(at), volume_lots=int(item.get("volume", 0)),
                       candle_source=item.get("candle_source", "CANDLE_SOURCE_UNSPECIFIED"))
            if bar["volume_lots"] >= 0:
                bars[at] = bar
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return [bars[at] for at in sorted(bars)]


def three_day_candles(daily, now=None):
    """3 calendar days, fixed UTC epoch anchor; only closed, fully covered windows.

    Non-trading dates have no fabricated OHLC or volume. The first window is
    omitted unless its start is covered by the actual native daily history.
    """
    if not daily:
        return []
    now = now or utcnow()
    width = 3 * 86400
    first = datetime.fromisoformat(daily[0]["time"].replace("Z", "+00:00")).timestamp()
    buckets = {}
    for bar in daily:
        at = datetime.fromisoformat(bar["time"].replace("Z", "+00:00")).timestamp()
        start = int(at // width) * width
        if start < first or start + width > now.timestamp():
            continue
        buckets.setdefault(start, []).append(bar)
    result = []
    for start, bars in sorted(buckets.items()):
        result.append({"time": iso(datetime.fromtimestamp(start, timezone.utc)),
            "open": bars[0]["open"], "high": max(b["high"] for b in bars),
            "low": min(b["low"] for b in bars), "close": bars[-1]["close"],
            "volume_lots": sum(b["volume_lots"] for b in bars), "source_bar_count": len(bars)})
    return result


class TBankConnection:
    def __init__(self, environ=None, factory=GrpcReader):
        self.env = os.environ if environ is None else environ
        self.factory = factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = self.stream_worker = self.history_worker = None
        self.history_lock = threading.Lock()
        self.history_error = None
        self.reader = None
        self.state = "WAITING_TOKEN"
        self.error = None
        self.checked_at = self.connected_at = None
        self.stream_state = "IDLE"
        self.stream_last_message_at = None
        self.stream_last_quote_at = None
        self.stream_last_book_at = None
        self.stream_last_status_at = None
        self.stream_reconnects = 0
        self.stream_error = None
        self.last_unary_recovery_at = None
        self.unary_recovery_count = 0
        self.instruments, self.instrument_errors, self.quotes, self.candles, self.books = {}, {}, {}, {}, {}
        self.accounts, self.portfolio = [], {}
        self.history_attempts = {}
        self.accounts_checked = self.resolve_checked = None
        self.stream_ids = ()
        self.trading_states = {}

    def _token(self):
        return self.env.get(TOKEN_ENV, "").strip()

    def _safe_connection_log(self, status, code=None, **fields):
        try:
            print(json.dumps({
                "event": "tbank_market_data_connection",
                "status": status,
                "code": code,
                **fields,
            }, separators=(",", ":")), flush=True)
        except Exception:
            pass

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            if not self._token():
                self.state = "WAITING_TOKEN"
                self._safe_connection_log("DISABLED", "TOKEN_MISSING",
                                          token_configured=False)
                return
            self.state = "CONNECTING"
            self._safe_connection_log("STARTING", None, token_configured=True)
            self.worker = threading.Thread(target=self._loop, name="veritas-tbank-readonly", daemon=True)
            self.worker.start()

    def status(self):
        with self.lock:
            checked_age = age_seconds(self.checked_at)
            state = self.state
            if state == "CONNECTED" and (checked_age is None or checked_age > 150):
                state = "STALE"
            return {"version": VERSION, "provider": "Т-Инвестиции", "protocol": "gRPC",
                    "mode": "READ_ONLY", "environment": "production", "status": state,
                    "token_configured": bool(self._token()), "token_variable": TOKEN_ENV,
                    "checked_at": self.checked_at, "connected_at": self.connected_at,
                    "error_code": self.error, "stream_status": self.stream_state,
                    "stream_health": {
                        "last_message_at": self.stream_last_message_at,
                        "last_message_age_seconds": age_seconds(self.stream_last_message_at),
                        "last_quote_at": self.stream_last_quote_at,
                        "last_book_at": self.stream_last_book_at,
                        "last_status_at": self.stream_last_status_at,
                        "reconnects": self.stream_reconnects,
                        "error_code": self.stream_error,
                        "unary_recovery_count": self.unary_recovery_count,
                        "last_unary_recovery_at": self.last_unary_recovery_at,
                    },
                    "orders_enabled": False, "paper_source_switch_enabled": self.env.get("VERITAS_CNY_PRIMARY_SOURCE","TBANK").upper()=="TBANK",
                    "instruments": {a: {"label": LABELS.get(a, a), "ticker": d.get("ticker"), "uid": d.get("uid"),
                        "name": d.get("name"), "exchange": d.get("exchange"), "real_exchange": d.get("real_exchange"),
                        "expiration_date": d.get("expiration_date"), "perpetual": d.get("perpetual", False),
                        "execution_selection": d.get("execution_selection"),
                        "execution_proxy_version": d.get("execution_proxy_version"),
                        "signal_asset": d.get("signal_asset"), "execution_asset": d.get("execution_asset"),
                        "minimum_expiry_buffer_seconds": d.get("minimum_expiry_buffer_seconds"),
                        "basic_asset": d.get("basic_asset"), "currency": d.get("currency"), "lot": d.get("lot"),
                        "min_price_increment": price(d.get("min_price_increment", {})),
                        "min_price_increment_amount": price(d.get("min_price_increment_amount", {})),
                        "status": "RESOLVED"} for a, d in self.instruments.items()},
                    "instrument_errors": dict(self.instrument_errors),
                    "requested_timeframes": list(TIMEFRAMES),
                    "timeframes": {a: {tf: self._history_status(a, tf) for tf in TIMEFRAMES} for a in self.instruments},
                    "private_data_protected": True}

    def _history_status(self, asset, interval):
        value = self.candles.get((asset, interval), {})
        age = age_seconds(value.get("loaded_at"))
        ttl = HISTORY["1d" if interval == "3d" else interval][2]
        state = value.get("status", "NOT_LOADED")
        if state == "OK" and (age is None or age > max(180, ttl * 3)):
            state = "STALE"
        bars = value.get("candles", [])
        return {"status": state, "count": len(bars), "loaded_at": value.get("loaded_at"),
                "last_candle_at": bars[-1]["time"] if bars else None, "error_code": value.get("error_code"),
                "aggregation": value.get("aggregation", "NATIVE"), "history_from": bars[0]["time"] if bars else None}

    def market_data(self):
        with self.lock:
            result = copy.deepcopy(self.quotes)
            books = copy.deepcopy(self.books)
        for q in result.values():
            age = age_seconds(q["observed_at"])
            q.update(age_seconds=age, stale=age is None or age > 120)
        for book in books.values():
            age = age_seconds(book.get("orderbook_ts"))
            book.update(age_seconds=age, stale=age is None or age > 120)
        return {"status": self.status()["status"], "quotes": result, "order_books": books}

    def private_snapshot(self, kind):
        with self.lock:
            data = self.accounts if kind == "accounts" else self.portfolio
            return {"status": self.status()["status"], "checked_at": self.checked_at,
                    kind: copy.deepcopy(data)}

    def candle_snapshot(self, asset, interval):
        asset = str(asset).upper()
        if interval not in TIMEFRAMES:
            return {"status": "INVALID_INTERVAL", "supported": list(TIMEFRAMES), "candles": []}
        with self.lock:
            value = copy.deepcopy(self.candles.get((asset, interval), {
                "status": "NOT_LOADED", "source": "TBANK_GRPC", "candles": []}))
            value["status"] = self._history_status(asset, interval)["status"]
            return value

    def _resolve(self):
        try:
            mapping = json.loads(self.env.get("TBANK_TICKERS", json.dumps(DEFAULT_TICKERS)))
        except (TypeError, ValueError):
            raise TBankError("INVALID_INSTRUMENT_CONFIGURATION") from None
        if not isinstance(mapping, dict) or len(mapping) > 7 or not all(
            isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 16 and 0 < len(v) <= 40
            for k, v in mapping.items()
        ):
            raise TBankError("INVALID_INSTRUMENT_CONFIGURATION")
        for asset, ticker in mapping.items():
            asset = asset.upper()
            if asset in self.instruments:
                continue
            try:
                instrument = (resolve_index_execution_future(self.reader, ticker)
                              if asset=="MOEXF"
                              else future_metadata(self.reader, exact_instrument(self.reader, ticker)))
                with self.lock:
                    self.instruments[asset] = instrument
                    self.instrument_errors.pop(asset, None)
            except TBankError as exc:
                with self.lock:
                    self.instrument_errors[asset] = str(exc)
        self.resolve_checked = iso()
        self._safe_connection_log(
            "INSTRUMENTS_RESOLVED", None,
            instruments_count=len(self.instruments),
            instrument_errors_count=len(self.instrument_errors),
            cny_resolved="CNYRUBF" in self.instruments,
        )

    def _read_accounts(self):
        accounts = self.reader.call("accounts", status="ACCOUNT_STATUS_OPEN").get("accounts", [])
        selected = self.env.get("TBANK_ACCOUNT_ID", "").strip()
        if not selected and len(accounts) == 1:
            selected = accounts[0]["id"]
        portfolio = {}
        if selected:
            if selected not in {a.get("id") for a in accounts}:
                raise TBankError("ACCOUNT_NOT_ACCESSIBLE")
            portfolio = self.reader.call("portfolio", account_id=selected)
        with self.lock:
            self.accounts, self.portfolio = accounts, portfolio
            self.accounts_checked = iso()

    def _read_market(self, *, include_history=True):
        by_uid = {data["uid"]: asset for asset, data in self.instruments.items()}
        if not by_uid:
            return
        reply = self.reader.call("prices", instrument_id=list(by_uid))
        for item in reply.get("last_prices", []):
            self._put_quote(item, by_uid)
        for uid, asset in by_uid.items():
            try:
                book = self.reader.call("book", instrument_id=uid, depth=10)
                if book.get("instrument_uid") == uid:
                    with self.lock:
                        self.books[asset] = {**book, "source": "TBANK_GRPC", "received_at": iso()}
                trading=self.reader.call("trading_status",instrument_id=uid)
                with self.lock:
                    self.trading_states[asset]={**trading,"checked_at":iso(),"instrument_uid":uid}
            except TBankError:
                pass  # Existing book keeps its original timestamp and becomes stale.
        if include_history:
            self._read_history(by_uid)

    def _read_history(self, by_uid):
        # Only one history owner; synchronous diagnostics may overlap the worker.
        if not self.history_lock.acquire(blocking=False):
            return
        try:
            self._read_history_locked(by_uid)
        finally:
            self.history_lock.release()

    def _read_history_locked(self, by_uid):
        jobs = []
        for interval, config in HISTORY.items():
            for uid, asset in by_uid.items():
                key = (asset, interval)
                old = self.candles.get(key, {})
                ttl = min(config[2], 300) if old.get("status") == "ERROR" else config[2]
                age = age_seconds(self.history_attempts.get(key))
                if age is None or age >= ttl:
                    jobs.append((key not in self.history_attempts, uid, asset, interval))
        # Cold intervals first, so minute updates cannot starve daily/weekly history.
        jobs.sort(key=lambda job: (not (job[2]=='CNYRUBF' and job[3] in ('1m','5m','1h')),not job[0]))
        started = time.monotonic()
        for _, uid, asset, interval in jobs[:12]:
            if time.monotonic() - started >= 30:
                break
            enum, days, _, limit = HISTORY[interval]
            key = (asset, interval)
            now = utcnow()
            self.history_attempts[key] = iso(now)
            try:
                start=now-timedelta(days=days); items=[]
                while start<now:
                    end=min(now,start+timedelta(days=7 if interval=='1h' else days))
                    reply=self.reader.call("candles",**{"instrument_id":uid,"interval":enum,"from":iso(start),"to":iso(end)})
                    items.extend(reply.get("candles",[]));start=end
                bars = closed_candles(items, now)
                value = {"status": "OK" if bars else "NO_DATA", "source": "TBANK_GRPC",
                    "instrument_uid": uid, "interval": interval, "loaded_at": iso(), "aggregation": "NATIVE",
                    "price_unit": "BROKER_NATIVE", "candle_source": "BROKER_DEFAULT_DEALER_BARS_EXCLUDED", "candles": bars[-limit:]}
                with self.lock:
                    self.candles[key] = value
                    if interval == "1d":
                        derived = three_day_candles(bars, now)
                        self.candles[(asset, "3d")] = {**value, "interval": "3d",
                            "status": "OK" if derived else "NO_DATA", "aggregation": "DAILY_3_CALENDAR_DAYS_UTC_EPOCH",
                            "candles": derived}
            except Exception as exc:
                code = str(exc) if isinstance(exc, TBankError) else "HISTORY_ERROR"
                with self.lock:
                    self.candles[key] = {**self.candles.get(key, {}), "status": "ERROR", "error_code": code}
                    if interval == "1d":
                        dkey = (asset, "3d")
                        self.candles[dkey] = {**self.candles.get(dkey, {}), "status": "ERROR", "error_code": code}

    def _put_quote(self, item, by_uid):
        uid = item.get("instrument_uid")
        if uid not in by_uid:
            return
        try:
            q = normalized_quote(item)
        except TBankError:
            return
        asset = by_uid[uid]
        with self.lock:
            now = utcnow()
            # Use the existing five-second clock-skew policy before replacing
            # last-good data. A future/invalid cached time must not pin recovery.
            if age_seconds(q["observed_at"], now) is None:
                return
            old = self.quotes.get(asset)
            if (old and age_seconds(old.get("observed_at"), now) is not None
                    and datetime.fromisoformat(q["observed_at"].replace("Z", "+00:00")) < datetime.fromisoformat(old["observed_at"].replace("Z", "+00:00"))):
                return
            self.quotes[asset] = q

    def _put_book(self, item, by_uid):
        uid = item.get("instrument_uid")
        if uid not in by_uid or item.get("is_consistent") is False:
            return
        observed = item.get("time") or item.get("orderbook_ts")
        if age_seconds(observed) is None:
            return
        bids, asks = item.get("bids", []), item.get("asks", [])
        if not isinstance(bids, list) or not isinstance(asks, list):
            return
        asset = by_uid[uid]
        with self.lock:
            now = utcnow()
            old = self.books.get(asset) or {}
            old_at = old.get("orderbook_ts") or old.get("time")
            try:
                older = (old_at is not None and age_seconds(old_at, now) is not None
                         and datetime.fromisoformat(observed.replace("Z", "+00:00"))
                         < datetime.fromisoformat(old_at.replace("Z", "+00:00")))
            except (AttributeError, TypeError, ValueError):
                older = False
            if older:
                return
            self.books[asset] = {
                **copy.deepcopy(item), "orderbook_ts": observed,
                "source": "TBANK_GRPC", "received_at": iso(now),
            }

    def _put_trading_status(self, item, by_uid):
        uid = item.get("instrument_uid")
        if uid not in by_uid:
            return
        asset = by_uid[uid]
        with self.lock:
            self.trading_states[asset] = {
                **copy.deepcopy(item), "instrument_uid": uid,
                "checked_at": iso(),
            }

    def _stream_healthy(self, now=None):
        now = now or utcnow()
        with self.lock:
            worker_alive = bool(self.stream_worker and self.stream_worker.is_alive())
            state = self.stream_state
            last = self.stream_last_message_at
        age = age_seconds(last, now)
        return bool(worker_alive and state == "SUBSCRIBED"
                    and age is not None and age <= STREAM_HEARTBEAT_MAX_AGE_SECONDS)

    def _market_recovery_needed(self, now=None):
        now = now or utcnow()
        if not self._stream_healthy(now):
            return True
        active = {
            "SECURITY_TRADING_STATUS_NORMAL_TRADING",
            "SECURITY_TRADING_STATUS_SESSION_OPEN",
            "SECURITY_TRADING_STATUS_OPENING_PERIOD",
        }
        with self.lock:
            instruments = tuple(self.instruments)
            quotes = copy.deepcopy(self.quotes)
            books = copy.deepcopy(self.books)
            states = copy.deepcopy(self.trading_states)
        for asset in instruments:
            if (states.get(asset) or {}).get("trading_status") not in active:
                continue
            quote_age = age_seconds((quotes.get(asset) or {}).get("observed_at"), now)
            book = books.get(asset) or {}
            book_age = age_seconds(book.get("orderbook_ts") or book.get("time"), now)
            if (quote_age is None or quote_age > STREAM_MARKET_RECOVERY_AGE_SECONDS
                    or book_age is None or book_age > STREAM_MARKET_RECOVERY_AGE_SECONDS):
                return True
        return False

    def _recover_market_unary(self, *, include_history=False):
        now = utcnow()
        with self.lock:
            previous = self.last_unary_recovery_at
        age = age_seconds(previous, now)
        if age is not None and age < STREAM_RECOVERY_COOLDOWN_SECONDS:
            return False
        self._read_market(include_history=include_history)
        with self.lock:
            self.last_unary_recovery_at = iso(now)
            self.unary_recovery_count += 1
        return True

    def _safe_stream_log(self, status, code=None):
        try:
            print(json.dumps({
                "event": "tbank_market_data_stream",
                "stream": "market_data_stream",
                "status": status,
                "code": code,
            }, separators=(",", ":")), flush=True)
        except Exception:
            pass

    def _stream_loop(self):
        attempt = 0
        while not self.stop_event.is_set():
            with self.lock:
                by_uid = {d["uid"]: a for a, d in self.instruments.items()}
            if not by_uid or self.reader is None:
                self.stop_event.wait(1)
                continue
            with self.lock:
                self.stream_state = "CONNECTING"
                self.stream_ids = tuple(sorted(by_uid))
                self.stream_error = None
            subscribed = set()
            try:
                self._safe_stream_log("CALL_OPEN")
                for item in self.reader.stream_market(
                        list(by_uid), depth=10, include_info=True,
                        ping_ms=STREAM_PING_MS):
                    if self.stop_event.is_set():
                        return
                    with self.lock:
                        self.stream_last_message_at = iso()
                    if tuple(sorted(d["uid"] for d in self.instruments.values())) != self.stream_ids:
                        break  # Re-subscribe when contract set changes.
                    checks = (
                        ("subscribe_last_price_response", "last_price_subscriptions", "last_price"),
                        ("subscribe_order_book_response", "order_book_subscriptions", "order_book"),
                        ("subscribe_info_response", "info_subscriptions", "info"),
                    )
                    for response_key, rows_key, label in checks:
                        response = item.get(response_key)
                        if response is None:
                            continue
                        rows = response.get(rows_key, [])
                        if (not rows or any(
                                row.get("subscription_status") != "SUBSCRIPTION_STATUS_SUCCESS"
                                for row in rows)):
                            raise TBankError("SUBSCRIPTION_REJECTED")
                        subscribed.add(label)
                    if {"last_price", "order_book", "info"} <= subscribed:
                        with self.lock:
                            was_subscribed = self.stream_state == "SUBSCRIBED"
                            self.stream_state = "SUBSCRIBED"
                            self.stream_error = None
                        if not was_subscribed:
                            self._safe_stream_log("SUBSCRIBED")
                        attempt = 0
                    if item.get("last_price"):
                        self._put_quote(item["last_price"], by_uid)
                        with self.lock:
                            self.stream_last_quote_at = iso()
                    if item.get("orderbook"):
                        self._put_book(item["orderbook"], by_uid)
                        with self.lock:
                            self.stream_last_book_at = iso()
                    if item.get("trading_status"):
                        self._put_trading_status(item["trading_status"], by_uid)
                        with self.lock:
                            self.stream_last_status_at = iso()
                if not self.stop_event.is_set():
                    raise TBankError("MARKET_DATA_STREAM_ENDED")
            except Exception as exc:
                code = str(exc) if isinstance(exc, TBankError) else "MARKET_DATA_STREAM_ERROR"
                with self.lock:
                    self.stream_state = "RECONNECTING"
                    self.stream_error = code
                    self.stream_reconnects += 1
                self._safe_stream_log("RECONNECTING", code)
                attempt += 1
                delay = min(STREAM_RECONNECT_MAX_SECONDS, .25 * (2 ** min(attempt, 5)))
                self.stop_event.wait(delay)

    def refresh(self, *, include_history=True):
        if self.accounts_checked is None or (age_seconds(self.accounts_checked) or 0) >= 60:
            self._read_accounts()
        if self.resolve_checked is None or (self.instrument_errors and (age_seconds(self.resolve_checked) or 0) >= 300):
            self._resolve()
        self._read_market(include_history=include_history)
        with self.lock:
            self.checked_at = iso()
            self.connected_at = self.connected_at or self.checked_at
            self.state, self.error = "CONNECTED", None

    def _ensure_market_workers(self):
        # Price streaming and book/session polling must not wait for a history
        # download. One bounded history worker uses the same read-only channel.
        with self.lock:
            if not self.instruments or self.stop_event.is_set():
                return
            for field, target, name in (
                    ('stream_worker', self._stream_loop, 'veritas-tbank-prices'),
                    ('history_worker', self._history_loop, 'veritas-tbank-history')):
                worker = getattr(self, field)
                if worker is None or not worker.is_alive():
                    worker = threading.Thread(target=target, name=name, daemon=True)
                    setattr(self, field, worker)
                    worker.start()
                    if field == 'stream_worker':
                        self._safe_connection_log(
                            "STREAM_WORKER_STARTED", None,
                            instruments_count=len(self.instruments),
                            cny_resolved="CNYRUBF" in self.instruments,
                        )

    def _history_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                by_uid = {data['uid']: asset for asset, data in self.instruments.items()}
            try:
                if by_uid and self.reader is not None:
                    self._read_history(by_uid)
                with self.lock:
                    self.history_error = None
            except Exception as exc:
                # Never disclose RPC metadata, tokens or account information.
                with self.lock:
                    self.history_error = str(exc) if isinstance(exc, TBankError) else 'HISTORY_ERROR'
            self.stop_event.wait(5)

    def _loop(self):
        delay = 15
        while not self.stop_event.is_set():
            try:
                if self.reader is None:
                    self.reader = self.factory(self._token())
                if self.checked_at is None:
                    # One unary bootstrap establishes exact instruments, account,
                    # first quote/book and session before the stream takes over.
                    self.refresh(include_history=False)
                else:
                    if self.accounts_checked is None or (age_seconds(self.accounts_checked) or 0) >= 60:
                        self._read_accounts()
                    if (self.resolve_checked is None
                            or (self.instrument_errors and (age_seconds(self.resolve_checked) or 0) >= 300)):
                        self._resolve()
                self._ensure_market_workers()
                if self.checked_at is not None and self._market_recovery_needed():
                    self._recover_market_unary(include_history=False)
                with self.lock:
                    self.checked_at = iso()
                    self.connected_at = self.connected_at or self.checked_at
                    self.state, self.error = "CONNECTED", None
                delay = 15
                self.stop_event.wait(5)
            except Exception as exc:
                code = str(exc) if isinstance(exc, TBankError) else "CONNECTION_ERROR"
                with self.lock:
                    self.state, self.error = "ERROR", code
                self._safe_connection_log(
                    "ERROR", code,
                    instruments_count=len(self.instruments),
                    cny_resolved="CNYRUBF" in self.instruments,
                )
                self.stop_event.wait(delay)
                delay = min(300, delay * 2)


connection = TBankConnection()


def status_page():
    s = connection.status()
    esc = lambda value: html.escape(str(value or "—"))
    heading = {"WAITING_TOKEN":"Ожидается токен Т-Банка", "CONNECTING":"Проверяется подключение",
               "CONNECTED":"Соединение установлено", "STALE":"Связь не подтверждена",
               "ERROR":"Не удалось подключиться"}.get(s["status"], s["status"])
    next_step = ("Добавьте секретную переменную TBANK_API_TOKEN в Environment сервиса VERITAS на Render. "
                 "Используйте токен «Только для чтения» из личного кабинета Т-Инвестиций.") if s["status"] == "WAITING_TOKEN" else ""
    cards = []
    headers = ("1м", "5м", "1ч", "4ч", "1д", "3д", "7д")
    names = {"OK":"готово", "NOT_LOADED":"загрузка", "NO_DATA":"нет данных", "ERROR":"ошибка", "STALE":"устарело"}
    for asset, instrument in s["instruments"].items():
        cells = []
        for tf in TIMEFRAMES:
            info = s["timeframes"][asset][tf]
            state = info["status"]
            cls = "ready" if state == "OK" else "waiting"
            cells.append(f'<td class="{cls}"><b>{info["count"]}</b><small>{names.get(state,state)}</small></td>')
        expiry = "Вечный фьючерс" if instrument["perpetual"] else "Экспирация: " + str(instrument.get("expiration_date") or "—")[:10]
        cards.append(f'<section><h2>{esc(instrument["label"])} <span>{esc(instrument["ticker"])}</span></h2>'
            f'<p>{esc(instrument["name"])}<br>Мосбиржа · {esc(expiry)}</p>'
            '<table><thead><tr>' + ''.join('<th>'+h+'</th>' for h in headers) + '</tr></thead><tbody><tr>'
            + ''.join(cells) + '</tr></tbody></table><p class="hint">Число — загруженные закрытые свечи.</p></section>')
    for asset, code in s["instrument_errors"].items():
        cards.append(f'<section><h2>{esc(LABELS.get(asset,asset))}</h2><p>Инструмент не подключён: {esc(code)}</p></section>')
    diagnostics = html.escape(json.dumps({"проверено_UTC":s["checked_at"],"код_ошибки":s["error_code"],
        "инструменты":s["instruments"],"ошибки_инструментов":s["instrument_errors"],
        "поток_цен":s["stream_status"],"таймфреймы":s["timeframes"]},ensure_ascii=False,indent=2))
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15"><title>Т-Инвестиции · VERITAS</title>
<style>body{{margin:0;padding:18px;background:#0b1118;color:#e7edf4;font:15px/1.5 system-ui}}main{{max-width:720px;margin:auto}}h1{{font-size:25px}}h2{{font-size:19px;margin:0}}h2 span{{float:right;color:#9fb0c0;font:14px/1.8 monospace}}a{{color:#93c5fd}}section{{padding:16px 12px;border:1px solid #35404c;border-radius:10px;margin:16px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}p{{overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse;table-layout:fixed;text-align:center;font-size:12px}}th{{color:#9fb0c0;padding:8px 0;border-bottom:1px solid #35404c}}td{{padding:10px 0}}td b{{font:600 15px monospace}}small{{display:block;font-size:9px}}.ready{{color:#52dfa4}}.waiting{{color:#ffc45b}}.hint{{font-size:12px;color:#9fb0c0}}</style>
<main><a href="/app">VERITAS</a><h1>Т-Инвестиции</h1><section><h2>{heading}</h2>
<p>Режим: только чтение · Python / gRPC</p><p>{next_step}</p>
<p>CNYRUBf используется учебными портфелями только после проверки точного контракта, котировки, стакана и истории. Реальные торговые поручения выключены.</p></section>
{''.join(cards)}<p class="hint">3д — закрытые трёхдневные календарные интервалы из дневных свечей, границы по UTC. 7д — недельные свечи брокера. История относится к указанному контракту; котировки других контрактов не добавляются.</p>
<details><summary>Диагностика</summary><pre>{diagnostics}</pre></details></main></html>'''
