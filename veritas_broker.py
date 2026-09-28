from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

import veritas_execution as VX

VERSION = "veritas-broker-contract-v1"


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


class LiveExecutionCoordinator:
    def __init__(self, adapter: BrokerAdapter):
        self.adapter = adapter

    def reconcile(self, expected_positions: Dict[str, float], tolerance: float = 1e-9) -> ReconciliationResult:
        actual = {p.asset: float(p.signed_quantity) for p in self.adapter.list_positions()}
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
        ok = bool(authorization_gate.get("eligible")) and not blockers
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
        if not preflight.get("eligible"):
            raise RuntimeError("LIVE_ORDER_BLOCKED:" + ",".join(preflight.get("blockers") or []))
        existing = self.adapter.get_order_by_client_id(intent.client_order_id)
        if existing is not None:
            return existing
        return self.adapter.submit_order(intent, float(quantity))
