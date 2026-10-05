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

VERSION = "R87_TBANK_THREE_FUTURES_ALL_TIMEFRAMES"
TARGET = "invest-public-api.tbank.ru:443"
PACKAGE = "tinkoff.public.invest.api.contract.v1"
TOKEN_ENV = "TBANK_API_TOKEN"
RPC_TIMEOUT = 8
DEFAULT_TICKERS = {"CNYRUBF": "CNYRUBF", "NQF": "NAZ6", "MOEXF": "IMOEXF"}
LABELS = {"CNYRUBF": "CNYRUBf", "NQF": "NQf", "MOEXF": "MOEXf"}
TIMEFRAMES = ("1m", "5m", "1h", "4h", "1d", "3d", "7d")
# Native history: enum, lookback days, refresh seconds, response cap.
HISTORY = {
    "1m": ("CANDLE_INTERVAL_1_MIN", 1, 20, 2400),
    "5m": ("CANDLE_INTERVAL_5_MIN", 7, 60, 2400),
    "1h": ("CANDLE_INTERVAL_HOUR", 90, 300, 2400),
    "4h": ("CANDLE_INTERVAL_4_HOUR", 90, 900, 700),
    "1d": ("CANDLE_INTERVAL_DAY", 5 * 365, 3600, 2400),
    "7d": ("CANDLE_INTERVAL_WEEK", 5 * 365, 3600, 300),
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
        if not item.get("is_complete"):
            continue
        try:
            at = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
            if at > now or at.tzinfo is None:
                continue
            bar = {key: price(item.get(key, {})) for key in ("open", "high", "low", "close")}
            if min(bar.values()) <= 0 or bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]):
                continue
            bar.update(time=iso(at), volume_lots=int(item.get("volume", 0)))
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
        self.worker = self.stream_worker = None
        self.reader = None
        self.state = "WAITING_TOKEN"
        self.error = None
        self.checked_at = self.connected_at = None
        self.stream_state = "IDLE"
        self.instruments, self.instrument_errors, self.quotes, self.candles, self.books = {}, {}, {}, {}, {}
        self.accounts, self.portfolio = [], {}
        self.history_attempts = {}
        self.accounts_checked = self.resolve_checked = None
        self.stream_ids = ()

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
                    "instruments": {a: {"label": LABELS.get(a, a), "ticker": d.get("ticker"), "uid": d.get("uid"),
                        "name": d.get("name"), "exchange": d.get("exchange"), "real_exchange": d.get("real_exchange"),
                        "expiration_date": d.get("expiration_date"), "perpetual": d.get("perpetual", False),
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
                instrument = future_metadata(self.reader, exact_instrument(self.reader, ticker))
                with self.lock:
                    self.instruments[asset] = instrument
                    self.instrument_errors.pop(asset, None)
            except TBankError as exc:
                with self.lock:
                    self.instrument_errors[asset] = str(exc)
        self.resolve_checked = iso()

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

    def _read_market(self):
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
            except TBankError:
                pass  # Existing book keeps its original timestamp and becomes stale.
        self._read_history(by_uid)

    def _read_history(self, by_uid):
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
        jobs.sort(key=lambda job: not job[0])
        started = time.monotonic()
        for _, uid, asset, interval in jobs[:12]:
            if time.monotonic() - started >= 30:
                break
            enum, days, _, limit = HISTORY[interval]
            key = (asset, interval)
            now = utcnow()
            self.history_attempts[key] = iso(now)
            try:
                reply = self.reader.call("candles", **{"instrument_id": uid, "interval": enum,
                    "from": iso(now - timedelta(days=days)), "to": iso(now), "limit": limit,
                    "candle_source_type": "CANDLE_SOURCE_EXCHANGE"})
                bars = closed_candles(reply.get("candles", []), now)
                value = {"status": "OK" if bars else "NO_DATA", "source": "TBANK_GRPC",
                    "instrument_uid": uid, "interval": interval, "loaded_at": iso(), "aggregation": "NATIVE",
                    "price_unit": "BROKER_NATIVE", "candle_source": "EXCHANGE", "candles": bars[-limit:]}
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
                self.stream_ids = tuple(sorted(by_uid))
            try:
                for item in self.reader.stream_prices(list(by_uid)):
                    if self.stop_event.is_set():
                        return
                    if tuple(sorted(d["uid"] for d in self.instruments.values())) != self.stream_ids:
                        break  # Re-subscribe when a previously unresolved contract becomes available.
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
        if self.accounts_checked is None or (age_seconds(self.accounts_checked) or 0) >= 60:
            self._read_accounts()
        if self.resolve_checked is None or (self.instrument_errors and (age_seconds(self.resolve_checked) or 0) >= 300):
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
                self.stop_event.wait(20)
            except Exception as exc:
                code = str(exc) if isinstance(exc, TBankError) else "CONNECTION_ERROR"
                with self.lock:
                    self.state, self.error = "ERROR", code
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
<p>Получение данных подключено отдельно от расчёта сигналов и учебных портфелей. Торговые поручения выключены.</p></section>
{''.join(cards)}<p class="hint">3д — закрытые трёхдневные календарные интервалы из дневных свечей, границы по UTC. 7д — недельные свечи брокера. История относится к указанному контракту; котировки других контрактов не добавляются.</p>
<details><summary>Диагностика</summary><pre>{diagnostics}</pre></details></main></html>'''
