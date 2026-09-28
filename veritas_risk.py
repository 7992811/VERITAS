from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "veritas-live-risk-v1"


@dataclass(frozen=True)
class PositionRisk:
    asset: str
    direction: str
    fraction_nav: float
    entry_price: float
    stop_price: float

    def stop_distance_pct(self) -> float:
        e=float(self.entry_price); s=float(self.stop_price)
        if e <= 0 or s <= 0:
            raise ValueError("invalid entry/stop")
        return abs(e-s)/e

    def stop_risk_nav(self) -> float:
        return abs(float(self.fraction_nav))*self.stop_distance_pct()


def _corr(correlations: Dict[str, Dict[str, float]], a: str, b: str) -> Optional[float]:
    if a==b:
        return 1.0
    try:
        if b in correlations.get(a,{}):
            return float(correlations[a][b])
        if a in correlations.get(b,{}):
            return float(correlations[b][a])
    except Exception:
        return None
    return None


def portfolio_stop_risk(positions: Iterable[PositionRisk],
                        correlations: Optional[Dict[str, Dict[str, float]]] = None,
                        corr_threshold: float = 0.65) -> Dict[str, Any]:
    rows=list(positions or [])
    blockers=[]
    risks={}
    for p in rows:
        try:
            r=p.stop_risk_nav()
        except Exception:
            blockers.append(f"INVALID_STOP:{p.asset}")
            continue
        risks[p.asset]=risks.get(p.asset,0.0)+r

    total=sum(risks.values())
    assets=sorted(risks)
    corr=correlations or {}
    if len(assets)>1 and not correlations:
        blockers.append("CORRELATION_MATRIX_REQUIRED")

    # Connected components of absolute correlation above threshold.
    seen=set()
    components=[]
    for a in assets:
        if a in seen:
            continue
        comp=set([a]); stack=[a]; seen.add(a)
        while stack:
            x=stack.pop()
            for y in assets:
                if y in seen:
                    continue
                c=_corr(corr,x,y)
                if c is not None and abs(c)>=float(corr_threshold):
                    seen.add(y); comp.add(y); stack.append(y)
        components.append(sorted(comp))

    component_rows=[]
    for comp in components:
        risk=sum(risks.get(a,0.0) for a in comp)
        component_rows.append({"assets":comp,"stop_risk_nav":risk})
    max_corr=max((x["stop_risk_nav"] for x in component_rows),default=0.0)

    return {
        "status":"BLOCK" if blockers else "PASS",
        "blockers":blockers,
        "total_open_stop_risk_nav":total,
        "max_correlated_stop_risk_nav":max_corr,
        "by_asset":risks,
        "correlation_components":component_rows,
        "correlation_threshold":float(corr_threshold),
        "positions":len(rows),
        "version":VERSION,
        "principle":"Risk is aggregated by stop loss and correlation, not by position count.",
    }


def with_proposed_position(current: Iterable[PositionRisk], proposed: PositionRisk,
                           correlations: Optional[Dict[str, Dict[str, float]]] = None,
                           corr_threshold: float = 0.65) -> Dict[str, Any]:
    rows=list(current or [])+[proposed]
    out=portfolio_stop_risk(rows,correlations,corr_threshold)
    out["proposed"]=asdict(proposed)
    out["proposed_stop_risk_nav"]=proposed.stop_risk_nav()
    return out
