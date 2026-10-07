from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import math
from typing import Dict, List, Optional

import veritas_execution as VX

VERSION = "veritas-broker-contract-v2"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACK = "ACK"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass
class BrokerOrder:
    client_order_id: str
    broker_order_id: Optional[str]
    asset: str
    side: str
    quantity: float
    filled_quantity: float
    avg_fill_price: Optional[float]
    status: OrderStatus


@dataclass
class BrokerPosition:
    asset: str
    signed_quantity: float
    mark_price: Optional[float] = None


@dataclass
class ReconciliationResult:
    ok: bool
    mismatches: List[Dict[str, float]]
    open_orders: int
    principle: str = "Any unexplained broker/local mismatch freezes new risk."


class BrokerAdapter(ABC):
    """Interface only. No broker is enabled in production until a concrete adapter passes sandbox tests."""

    @abstractmethod
    def heartbeat(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_positions(self) -> List[BrokerPosition]:
        raise NotImplementedError

    @abstractmethod
    def list_open_orders(self) -> List[BrokerOrder]:
        raise NotImplementedError

    @abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, intent: VX.OrderIntent, quantity: float) -> BrokerOrder:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError


def _finite_quantity(value, label):
    # NaN comparisons are false: abs(expected-observed)>tolerance alone cannot
    # establish that a broker snapshot matches the local book.
    if isinstance(value, bool):
        raise ValueError(label + ': boolean is not a quantity')
    try:
        quantity = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(label + ': invalid quantity') from exc
    if not math.isfinite(quantity):
        raise ValueError(label + ': non-finite quantity')
    return quantity


def _instrument(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('BROKER_INSTRUMENT_MISSING')
    return value


class LiveExecutionCoordinator:
    def __init__(self, adapter: BrokerAdapter):
        self.adapter = adapter

    def reconcile(self, expected_positions: Dict[str, float], tolerance: float = 1e-9) -> ReconciliationResult:
        tolerance = _finite_quantity(tolerance, 'RECONCILIATION_TOLERANCE')
        if tolerance < 0:
            raise ValueError('RECONCILIATION_TOLERANCE: negative tolerance')
        expected_positions = {_instrument(asset): _finite_quantity(value, 'EXPECTED_POSITION')
                              for asset, value in expected_positions.items()}
        actual = {}
        for position in self.adapter.list_positions():
            asset = _instrument(position.asset)
            if asset in actual:
                # This interface requires one net position per instrument. Do
                # not silently choose the final row of an ambiguous snapshot.
                raise ValueError('BROKER_DUPLICATE_POSITION:' + asset)
            actual[asset] = _finite_quantity(position.signed_quantity, 'BROKER_POSITION')
        assets = sorted(set(expected_positions) | set(actual))
        mismatches = []
        for asset in assets:
            expected = float(expected_positions.get(asset, 0.0))
            observed = float(actual.get(asset, 0.0))
            if abs(expected - observed) > tolerance:
                mismatches.append({
                    "asset": asset,
                    "expected_quantity": expected,
                    "broker_quantity": observed,
                    "difference": observed - expected,
                })
        orders = self.adapter.list_open_orders()
        return ReconciliationResult(ok=not mismatches, mismatches=mismatches, open_orders=len(orders))

    def preflight(self, authorization_gate: Dict, reconciliation: ReconciliationResult) -> Dict:
        blockers = list(authorization_gate.get("blockers") or [])
        if not self.adapter.heartbeat():
            blockers.append("BROKER_HEARTBEAT_FAILED")
        if not reconciliation.ok:
            blockers.append("BROKER_POSITION_MISMATCH")
        ok = authorization_gate.get("eligible") is True and not blockers
        return {
            "eligible": ok,
            "status": "PASS" if ok else "BLOCK",
            "blockers": list(dict.fromkeys(blockers)),
            "reconciliation": {
                "ok": reconciliation.ok,
                "mismatches": reconciliation.mismatches,
                "open_orders": reconciliation.open_orders,
            },
            "broker_contract_version": VERSION,
        }

    def submit_once(self, intent: VX.OrderIntent, quantity: float, preflight: Dict) -> BrokerOrder:
        if preflight.get("eligible") is not True:
            raise RuntimeError("LIVE_ORDER_BLOCKED:" + ",".join(preflight.get("blockers") or []))
        quantity = _finite_quantity(quantity, 'ORDER_QUANTITY')
        if quantity <= 0:
            raise ValueError('ORDER_QUANTITY: positive quantity required')
        if not isinstance(intent.client_order_id, str) or not intent.client_order_id.strip():
            raise ValueError('CLIENT_ORDER_ID_MISSING')
        existing = self.adapter.get_order_by_client_id(intent.client_order_id)
        if existing is not None:
            try:
                original_quantity = _finite_quantity(existing.quantity, 'EXISTING_ORDER_QUANTITY')
                filled_quantity = _finite_quantity(existing.filled_quantity, 'EXISTING_FILLED_QUANTITY')
                if original_quantity <= 0 or not 0 <= filled_quantity <= original_quantity:
                    raise ValueError('inconsistent existing fill')
                if existing.status not in set(OrderStatus):
                    raise ValueError('unrecognized order status')
                if filled_quantity > 0 and _finite_quantity(existing.avg_fill_price, 'EXISTING_FILL_PRICE') <= 0:
                    raise ValueError('invalid existing fill price')
            except (TypeError, ValueError) as exc:
                raise RuntimeError('BROKER_ORDER_STATE_INVALID') from exc
            if (existing.client_order_id != intent.client_order_id or existing.asset != intent.asset
                    or existing.side != intent.side or original_quantity != quantity):
                raise RuntimeError('CLIENT_ORDER_ID_CONFLICT')
            # UNKNOWN is returned as UNKNOWN, never treated as a missing order
            # and never authorized for a fresh submission with the same ID.
            return existing
        return self.adapter.submit_order(intent, quantity)
