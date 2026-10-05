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

VERSION = "R83_TBANK_READ_ONLY"
TARGET = "invest-public-api.tbank.ru:443"
PACKAGE = "tinkoff.public.invest.api.contract.v1"
TOKEN_ENV = "TBANK_API_TOKEN"
RPC_TIMEOUT = 8
METHODS = {
    "accounts": ("UsersService/GetAccounts", "GetAccountsRequest", "GetAccountsResponse"),
    "find": ("InstrumentsService/FindInstrument", "FindInstrumentRequest", "FindInstrumentResponse"),
    "instrument": ("InstrumentsService/GetInstrumentBy", "InstrumentRequest", "InstrumentResponse"),
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

    def stream_prices(self, ids):
        req = self.parse({"subscribe_last_price_request": {
            "subscription_action": "SUBSCRIPTION_ACTION_SUBSCRIBE",
            "instruments": [{"instrument_id": uid} for uid in ids],
        }}, self.proto.MarketDataServerSideStreamRequest())
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


def normalized_quote(item, received_at=None):
    value = price(item.get("price", {}))
    if value <= 0 or not item.get("instrument_uid") or not item.get("time"):
        raise TBankError("INVALID_QUOTE")
    return {"instrument_uid": item["instrument_uid"], "price": value,
            "observed_at": item["time"], "received_at": received_at or iso(),
            "source": "TBANK_GRPC", "price_unit": "BROKER_NATIVE"}


class TBankConnection:
    def __init__(self, environ=None, factory=GrpcReader):
        self.env = os.environ if environ is None else environ
        self.factory = factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = self.stream_worker = None
        self.reader = None
        self.state = "WAITING_TOKEN"
        self.error = None
        self.checked_at = self.connected_at = None
        self.stream_state = "IDLE"
        self.instruments, self.instrument_errors, self.quotes, self.candles, self.books = {}, {}, {}, {}, {}
        self.accounts, self.portfolio = [], {}

    def _token(self):
        return self.env.get(TOKEN_ENV, "").strip()

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            if not self._token():
                self.state = "WAITING_TOKEN"
                return
            self.state = "CONNECTING"
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
                    "orders_enabled": False, "paper_source_switch_enabled": False,
                    "instruments": {a: {"ticker": d.get("ticker"), "uid": d.get("uid"),
                                         "status": "RESOLVED"} for a, d in self.instruments.items()},
                    "instrument_errors": dict(self.instrument_errors),
                    "private_data_protected": True}

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
        with self.lock:
            return copy.deepcopy(self.candles.get((asset, interval), {
                "status": "NOT_LOADED", "source": "TBANK_GRPC", "candles": []}))

    def _resolve(self):
        try:
            mapping = json.loads(self.env.get("TBANK_TICKERS", '{"CNYRUBF":"CNYRUBF"}'))
        except (TypeError, ValueError):
            raise TBankError("INVALID_INSTRUMENT_CONFIGURATION") from None
        if not isinstance(mapping, dict) or len(mapping) > 7 or not all(
            isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 16 and 0 < len(v) <= 40
            for k, v in mapping.items()
        ):
            raise TBankError("INVALID_INSTRUMENT_CONFIGURATION")
        for asset, ticker in mapping.items():
            try:
                instrument = exact_instrument(self.reader, ticker)
                with self.lock:
                    self.instruments[asset] = instrument
                    self.instrument_errors.pop(asset, None)
            except TBankError as exc:
                with self.lock:
                    self.instrument_errors[asset] = str(exc)

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

    def _read_market(self):
        by_uid = {data["uid"]: asset for asset, data in self.instruments.items()}
        if not by_uid:
            return
        reply = self.reader.call("prices", instrument_id=list(by_uid))
        for item in reply.get("last_prices", []):
            self._put_quote(item, by_uid)
        for uid, asset in by_uid.items():
            book = self.reader.call("book", instrument_id=uid, depth=10)
            if book.get("instrument_uid") == uid:
                with self.lock:
                    self.books[asset] = {**book, "source": "TBANK_GRPC", "received_at": iso()}
            now = utcnow()
            for interval, enum_name, days in (("5m", "CANDLE_INTERVAL_5_MIN", 1), ("1h", "CANDLE_INTERVAL_HOUR", 7)):
                reply = self.reader.call("candles", **{"instrument_id": uid, "interval": enum_name,
                                        "from": iso(now - timedelta(days=days)), "to": iso(now)})
                candles = []
                for c in reply.get("candles", []):
                    if c.get("is_complete") and c.get("time"):
                        candles.append({"time": c["time"], "open": price(c.get("open", {})),
                            "high": price(c.get("high", {})), "low": price(c.get("low", {})),
                            "close": price(c.get("close", {})), "volume_lots": int(c.get("volume", 0))})
                with self.lock:
                    self.candles[(asset, interval)] = {"status": "OK", "source": "TBANK_GRPC",
                        "instrument_uid": uid, "interval": interval, "loaded_at": iso(),
                        "price_unit": "BROKER_NATIVE", "candles": sorted(candles, key=lambda c: c["time"])[-2000:]}

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
            old = self.quotes.get(asset)
            if old and datetime.fromisoformat(q["observed_at"].replace("Z", "+00:00")) < datetime.fromisoformat(old["observed_at"].replace("Z", "+00:00")):
                return
            self.quotes[asset] = q

    def _stream_loop(self):
        while not self.stop_event.is_set():
            by_uid = {d["uid"]: a for a, d in self.instruments.items()}
            if not by_uid or self.reader is None:
                self.stop_event.wait(15)
                continue
            with self.lock:
                self.stream_state = "CONNECTING"
            try:
                for item in self.reader.stream_prices(list(by_uid)):
                    if self.stop_event.is_set():
                        return
                    response = item.get("subscribe_last_price_response")
                    if response is not None:
                        subscriptions = response.get("last_price_subscriptions", [])
                        if not subscriptions or any(s.get("subscription_status") != "SUBSCRIPTION_STATUS_SUCCESS" for s in subscriptions):
                            raise TBankError("SUBSCRIPTION_REJECTED")
                        with self.lock:
                            self.stream_state = "SUBSCRIBED"
                    if item.get("last_price"):
                        self._put_quote(item["last_price"], by_uid)
                with self.lock:
                    self.stream_state = "RECONNECTING"
            except Exception:
                with self.lock:
                    self.stream_state = "RECONNECTING"
            self.stop_event.wait(5)

    def refresh(self):
        self._read_accounts()
        if not self.instruments:
            self._resolve()
        self._read_market()
        with self.lock:
            self.checked_at = iso()
            self.connected_at = self.connected_at or self.checked_at
            self.state, self.error = "CONNECTED", None

    def _loop(self):
        delay = 15
        while not self.stop_event.is_set():
            try:
                if self.reader is None:
                    self.reader = self.factory(self._token())
                self.refresh()
                if self.stream_worker is None and self.instruments:
                    self.stream_worker = threading.Thread(target=self._stream_loop, name="veritas-tbank-prices", daemon=True)
                    self.stream_worker.start()
                delay = 15
                self.stop_event.wait(60)
            except Exception as exc:
                code = str(exc) if isinstance(exc, TBankError) else "CONNECTION_ERROR"
                with self.lock:
                    self.state, self.error = "ERROR", code
                self.stop_event.wait(delay)
                delay = min(300, delay * 2)


connection = TBankConnection()


def status_page():
    s = connection.status()
    heading = {"WAITING_TOKEN":"Ожидается токен Т-Банка", "CONNECTING":"Проверяется подключение",
               "CONNECTED":"Соединение установлено", "STALE":"Связь не подтверждена",
               "ERROR":"Не удалось подключиться"}.get(s["status"], s["status"])
    next_step = ("Добавьте секретную переменную TBANK_API_TOKEN в Environment сервиса VERITAS на Render. "
                 "Используйте токен «Только для чтения» из личного кабинета Т-Инвестиций.") if s["status"] == "WAITING_TOKEN" else ""
    diagnostics = html.escape(json.dumps({"проверено_UTC":s["checked_at"],"код_ошибки":s["error_code"],
        "инструменты":s["instruments"],"ошибки_инструментов":s["instrument_errors"],
        "поток_цен":s["stream_status"]},ensure_ascii=False,indent=2))
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15"><title>Т-Инвестиции · VERITAS</title>
<style>body{{margin:0;padding:24px;background:#0e1720;color:#e7edf4;font:16px/1.55 system-ui}}main{{max-width:680px;margin:auto}}h1{{font-size:26px}}h2{{font-size:20px}}a{{color:#93c5fd}}section{{padding:20px;border:1px solid #2b3845;border-radius:16px;margin:20px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}}p{{overflow-wrap:anywhere}}</style>
<main><a href="/app">VERITAS</a><h1>Т-Инвестиции</h1><section><h2>{heading}</h2>
<p>Режим: только чтение<br>Подключение: Python / gRPC</p><p>{next_step}</p>
<p>Торговые поручения выключены. Данные подключения хранятся отдельно от учебных портфелей.</p></section>
<details><summary>Диагностика</summary><pre>{diagnostics}</pre></details></main></html>'''
