"""Low-latency T-Bank order/fill stream observer for VERITAS Currency.

The stream is read-only. It never submits, cancels, replaces, approves or
requeues an order. OrderStateStream and TradesStream are used only as immediate
wake-up signals for the existing canonical reconciliation path, which still
confirms broker state through GetOrderState before accounting anything.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import queue
import re
import ssl
import threading
import time

import grpc

from tinkoff.invest.grpc import common_pb2, orders_pb2, orders_pb2_grpc


TARGETS = {
    "production": "invest-public-api.tbank.ru:443",
    "sandbox": "sandbox-invest-public-api.tbank.ru:443",
}
CNY_UID = "c300543d-aa18-4249-b110-615409dde036"
APP_NAME = "7992811.VERITAS"
MAX_SEEN = 2048
PING_MS = 5000


class OrderStreamError(RuntimeError):
    pass


def _text(value, code, limit=256):
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > limit:
        raise OrderStreamError(code)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise OrderStreamError(code)
    return value


def _iso_timestamp(value):
    if value is None:
        return None
    try:
        dt = value.ToDatetime(tzinfo=timezone.utc)
    except Exception:
        return None
    return dt.isoformat()


def _decimal(value):
    if value is None:
        return None
    try:
        return str(Decimal(value.units) + (Decimal(value.nano) / Decimal(1_000_000_000)))
    except Exception:
        return None


def _enum_name(enum_wrapper, value):
    try:
        return enum_wrapper.Name(value)
    except Exception:
        return "UNRECOGNIZED_" + str(value)


class TBankOrderEventStream:
    """Resilient read-side observer for one account/instrument pair."""

    def __init__(self, token, account_id, *, instrument_uid=CNY_UID,
                 environment="production", on_event=None, channel=None, log=None):
        if not isinstance(token, str) or not token or any(ch.isspace() for ch in token):
            raise OrderStreamError("INVALID_TOKEN_ARGUMENT")
        self.account_id = _text(account_id, "INVALID_ACCOUNT_ID")
        self.instrument_uid = _text(instrument_uid, "INVALID_INSTRUMENT_UID", 64)
        if environment not in TARGETS:
            raise OrderStreamError("INVALID_ENVIRONMENT")
        if on_event is not None and not callable(on_event):
            raise OrderStreamError("INVALID_STREAM_CALLBACK")
        self.environment = environment
        self.target = TARGETS[environment]
        self.on_event = on_event
        self.log = log if callable(log) else (lambda *_args, **_kwargs: None)
        self._metadata = (
            ("authorization", "Bearer " + token),
            ("x-app-name", APP_NAME),
        )
        self._owns_channel = channel is None
        self._channel = channel or self._make_channel()
        self._stop = threading.Event()
        self._wake = queue.Queue(maxsize=1)
        self._threads = []
        self._calls = set()
        self._lock = threading.RLock()
        self._seen_order = deque()
        self._seen_order_set = set()
        self._seen_trade = deque()
        self._seen_trade_set = set()
        self._latest_event = None
        self._state = {
            "started": False,
            "order_state_stream": "IDLE",
            "trades_stream": "IDLE",
            "last_order_event_at": None,
            "last_trade_event_at": None,
            "last_subscription_at": None,
            "last_reconcile_at": None,
            "last_error": None,
            "reconnects": 0,
        }

    def _make_channel(self):
        roots = b"".join(
            ssl.DER_cert_to_PEM_cert(cert).encode()
            for cert in ssl.create_default_context().get_ca_certs(binary_form=True)
        )
        extra = Path(__file__).with_name("vendor").joinpath(
            "tbank", "russian_trusted_root_ca.pem")
        if extra.is_file():
            roots += extra.read_bytes()
        return grpc.secure_channel(
            self.target,
            grpc.ssl_channel_credentials(roots),
            options=(
                ("grpc.max_receive_message_length", 4 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 20000),
                ("grpc.keepalive_timeout_ms", 5000),
                ("grpc.keepalive_permit_without_calls", 1),
            ),
        )

    def __repr__(self):
        return "TBankOrderEventStream(" + self.environment + ", READ_ONLY)"

    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **changes):
        with self._lock:
            self._state.update(changes)

    def _remember(self, kind, fingerprint):
        dq = self._seen_order if kind == "ORDER_STATE" else self._seen_trade
        seen = self._seen_order_set if kind == "ORDER_STATE" else self._seen_trade_set
        with self._lock:
            if fingerprint in seen:
                return False
            seen.add(fingerprint)
            dq.append(fingerprint)
            while len(dq) > MAX_SEEN:
                seen.discard(dq.popleft())
            return True

    def _fingerprint(self, event):
        return hashlib.sha256(json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()).hexdigest()

    def _enqueue(self, event):
        fingerprint = self._fingerprint(event)
        if not self._remember(event["kind"], fingerprint):
            return
        with self._lock:
            self._latest_event = dict(event)
        try:
            self._wake.put_nowait(True)
        except queue.Full:
            pass

    def _safe_log(self, stream_name, status, code=None):
        try:
            self.log(json.dumps({
                "event": "tbank_order_event_stream",
                "environment": self.environment,
                "stream": stream_name,
                "status": status,
                "code": code,
            }, separators=(",", ":")))
        except Exception:
            pass

    def _subscription(self, stream_name, sub):
        now = datetime.now(timezone.utc).isoformat()
        ok = sub.status == common_pb2.RESULT_SUBSCRIPTION_STATUS_OK
        status = "SUBSCRIBED" if ok else "SUBSCRIPTION_ERROR"
        self._set(**{
            stream_name: status,
            "last_subscription_at": now,
            "last_error": None if ok else "SUBSCRIPTION_REJECTED",
        })
        self._safe_log(stream_name, status, None if ok else "SUBSCRIPTION_REJECTED")

    def _order_event(self, state):
        if state.account_id != self.account_id or state.instrument_uid != self.instrument_uid:
            return None
        request_id = state.order_request_id if state.HasField("order_request_id") else None
        return {
            "kind": "ORDER_STATE",
            "account_id": state.account_id,
            "instrument_uid": state.instrument_uid,
            "broker_order_id": state.order_id or None,
            "client_order_id": request_id,
            "status": _enum_name(orders_pb2.OrderExecutionReportStatus,
                                 state.execution_report_status),
            "direction": _enum_name(orders_pb2.OrderDirection, state.direction),
            "lots_requested": int(state.lots_requested),
            "lots_executed": int(state.lots_executed),
            "lots_left": int(state.lots_left),
            "lots_cancelled": int(state.lots_cancelled),
            "order_price": _decimal(state.order_price),
            "executed_order_price": _decimal(state.executed_order_price),
            "created_at": _iso_timestamp(state.created_at),
            "completion_time": _iso_timestamp(state.completion_time),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }

    def _trade_event(self, order_trades):
        if (order_trades.account_id != self.account_id
                or order_trades.instrument_uid != self.instrument_uid):
            return None
        trades = [{
            "trade_id": trade.trade_id,
            "quantity": int(trade.quantity),
            "price": _decimal(trade.price),
            "executed_at": _iso_timestamp(trade.date_time),
        } for trade in order_trades.trades]
        return {
            "kind": "TRADE",
            "account_id": order_trades.account_id,
            "instrument_uid": order_trades.instrument_uid,
            "broker_order_id": order_trades.order_id or None,
            "direction": _enum_name(orders_pb2.OrderDirection,
                                    order_trades.direction),
            "trades": trades,
            "created_at": _iso_timestamp(order_trades.created_at),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }

    def _register_call(self, call):
        with self._lock:
            self._calls.add(call)

    def _unregister_call(self, call):
        with self._lock:
            self._calls.discard(call)

    def _backoff(self, attempt):
        self._stop.wait(min(5.0, max(0.25, 0.25 * (2 ** min(attempt, 4)))))

    def _run_order_states(self):
        attempt = 0
        stub = orders_pb2_grpc.OrdersStreamServiceStub(self._channel)
        while not self._stop.is_set():
            call = None
            try:
                self._set(order_state_stream="CONNECTING")
                request = orders_pb2.OrderStateStreamRequest(
                    accounts=[self.account_id], ping_delay_millis=PING_MS)
                call = stub.OrderStateStream(
                    request, metadata=self._metadata, timeout=300, wait_for_ready=True)
                self._register_call(call)
                self._set(order_state_stream="CONNECTED", last_error=None)
                attempt = 0
                for response in call:
                    if self._stop.is_set():
                        break
                    payload = response.WhichOneof("payload")
                    if payload == "subscription":
                        self._subscription("order_state_stream", response.subscription)
                    elif payload == "order_state":
                        event = self._order_event(response.order_state)
                        if event is not None:
                            self._set(last_order_event_at=event["received_at"])
                            self._enqueue(event)
                if not self._stop.is_set():
                    raise OrderStreamError("ORDER_STATE_STREAM_ENDED")
            except grpc.RpcError as exc:
                if self._stop.is_set():
                    break
                code = exc.code().name if hasattr(exc, "code") else "RPC_ERROR"
                self._set(order_state_stream="RECONNECTING",
                          last_error="ORDER_STATE_" + code,
                          reconnects=self.status()["reconnects"] + 1)
                self._safe_log("order_state_stream", "RECONNECTING", "ORDER_STATE_" + code)
            except Exception as exc:
                if self._stop.is_set():
                    break
                code = str(exc)
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                    code = "ORDER_STATE_STREAM_ERROR"
                self._set(order_state_stream="RECONNECTING", last_error=code,
                          reconnects=self.status()["reconnects"] + 1)
                self._safe_log("order_state_stream", "RECONNECTING", code)
            finally:
                if call is not None:
                    self._unregister_call(call)
            attempt += 1
            self._backoff(attempt)
        self._set(order_state_stream="STOPPED")

    def _run_trades(self):
        attempt = 0
        stub = orders_pb2_grpc.OrdersStreamServiceStub(self._channel)
        while not self._stop.is_set():
            call = None
            try:
                self._set(trades_stream="CONNECTING")
                request = orders_pb2.TradesStreamRequest(
                    accounts=[self.account_id], ping_delay_ms=PING_MS)
                call = stub.TradesStream(
                    request, metadata=self._metadata, timeout=300, wait_for_ready=True)
                self._register_call(call)
                self._set(trades_stream="CONNECTED", last_error=None)
                attempt = 0
                for response in call:
                    if self._stop.is_set():
                        break
                    payload = response.WhichOneof("payload")
                    if payload == "subscription":
                        self._subscription("trades_stream", response.subscription)
                    elif payload == "order_trades":
                        event = self._trade_event(response.order_trades)
                        if event is not None:
                            self._set(last_trade_event_at=event["received_at"])
                            self._enqueue(event)
                if not self._stop.is_set():
                    raise OrderStreamError("TRADES_STREAM_ENDED")
            except grpc.RpcError as exc:
                if self._stop.is_set():
                    break
                code = exc.code().name if hasattr(exc, "code") else "RPC_ERROR"
                self._set(trades_stream="RECONNECTING",
                          last_error="TRADES_" + code,
                          reconnects=self.status()["reconnects"] + 1)
                self._safe_log("trades_stream", "RECONNECTING", "TRADES_" + code)
            except Exception as exc:
                if self._stop.is_set():
                    break
                code = str(exc)
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                    code = "TRADES_STREAM_ERROR"
                self._set(trades_stream="RECONNECTING", last_error=code,
                          reconnects=self.status()["reconnects"] + 1)
                self._safe_log("trades_stream", "RECONNECTING", code)
            finally:
                if call is not None:
                    self._unregister_call(call)
            attempt += 1
            self._backoff(attempt)
        self._set(trades_stream="STOPPED")

    def _dispatch(self):
        while not self._stop.is_set():
            try:
                self._wake.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break
            callback = self.on_event
            if callback is None:
                continue
            with self._lock:
                event = dict(self._latest_event or {})
            try:
                callback(event)
                self._set(last_reconcile_at=datetime.now(timezone.utc).isoformat(),
                          last_error=None)
            except Exception:
                self._set(last_error="STREAM_RECONCILIATION_FAILED")

    def start(self):
        with self._lock:
            if self._state["started"]:
                return self
            self._state["started"] = True
        self._threads = [
            threading.Thread(target=self._run_order_states,
                             name="veritas-tbank-order-state-stream", daemon=True),
            threading.Thread(target=self._run_trades,
                             name="veritas-tbank-trades-stream", daemon=True),
            threading.Thread(target=self._dispatch,
                             name="veritas-tbank-stream-reconcile", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def stop(self, timeout=3.0):
        self._stop.set()
        with self._lock:
            calls = list(self._calls)
        for call in calls:
            try:
                call.cancel()
            except Exception:
                pass
        try:
            self._wake.put_nowait(True)
        except queue.Full:
            pass
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._owns_channel:
            try:
                self._channel.close()
            except Exception:
                pass
        self._set(started=False)
