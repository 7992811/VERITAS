from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

VERSION = "veritas-instrument-contract-v1"


@dataclass(frozen=True)
class InstrumentSpec:
    asset: str
    venue: str
    instrument_id: str
    instrument_type: str
    quote_currency: str
    pnl_currency: str
    tick_size: float
    lot_size: float
    contract_multiplier: float
    min_quantity: float
    source: str
    observed_at: str
    expiry: Optional[str] = None

    def validate(self) -> Dict[str, Any]:
        blockers=[]
        if not self.asset: blockers.append("ASSET_MISSING")
        if not self.venue: blockers.append("VENUE_MISSING")
        if not self.instrument_id: blockers.append("INSTRUMENT_ID_MISSING")
        if not self.quote_currency: blockers.append("QUOTE_CURRENCY_MISSING")
        if not self.pnl_currency: blockers.append("PNL_CURRENCY_MISSING")
        if self.tick_size <= 0: blockers.append("TICK_SIZE_INVALID")
        if self.lot_size <= 0: blockers.append("LOT_SIZE_INVALID")
        if self.contract_multiplier <= 0: blockers.append("CONTRACT_MULTIPLIER_INVALID")
        if self.min_quantity <= 0: blockers.append("MIN_QUANTITY_INVALID")
        if not self.source: blockers.append("SPEC_SOURCE_MISSING")
        if not self.observed_at: blockers.append("SPEC_TIMESTAMP_MISSING")
        ok=not blockers
        return {"valid":ok,"status":"PASS" if ok else "BLOCK",
                "blockers":blockers,"spec":asdict(self),"version":VERSION}


def quantity_for_notional(spec: InstrumentSpec, notional_quote_ccy: float, price: float) -> Dict[str, Any]:
    v=spec.validate()
    if not v["valid"]:
        return {"status":"BLOCK","blockers":v["blockers"]}
    px=float(price)
    target=float(notional_quote_ccy)
    if px <= 0 or target <= 0:
        return {"status":"BLOCK","blockers":["PRICE_OR_NOTIONAL_INVALID"]}
    unit_notional=px*spec.contract_multiplier*spec.lot_size
    raw=target/unit_notional
    lots=int(raw)
    qty=max(0.0,lots*spec.lot_size)
    if qty < spec.min_quantity:
        return {"status":"BLOCK","blockers":["BELOW_MIN_QUANTITY"],
                "raw_quantity":raw,"rounded_quantity":qty}
    realized=qty*px*spec.contract_multiplier
    return {"status":"PASS","quantity":qty,"raw_quantity":raw,
            "realized_notional":realized,
            "unit_notional":unit_notional,
            "principle":"Live sizing uses broker/exchange contract metadata, never price-only normalized units."}


class InstrumentRegistry:
    """Runtime registry populated by a broker/market-data adapter, not hard-coded research symbols."""
    def __init__(self):
        self._items: Dict[str, InstrumentSpec]={}

    def put(self, spec: InstrumentSpec):
        check=spec.validate()
        if not check["valid"]:
            raise ValueError("INVALID_INSTRUMENT_SPEC:"+",".join(check["blockers"]))
        self._items[spec.asset]=spec

    def get(self, asset: str) -> Optional[InstrumentSpec]:
        return self._items.get(str(asset or ""))

    def readiness(self, asset: str) -> Dict[str, Any]:
        spec=self.get(asset)
        if spec is None:
            return {"ready":False,"status":"BLOCK","blockers":["INSTRUMENT_SPEC_NOT_LOADED"],"version":VERSION}
        v=spec.validate()
        return {"ready":bool(v["valid"]),"status":v["status"],"blockers":v["blockers"],
                "instrument_id":spec.instrument_id,"venue":spec.venue,"version":VERSION}
