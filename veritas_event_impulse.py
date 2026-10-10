"""Causal intrabar volatility-shock evidence for event-impulse execution.

Uses only the current verified quote plus volatility estimates from observations
already complete before that quote. It never waits for the forming candle to
close and never fabricates volume or news confirmation.
"""
from __future__ import annotations

import math

VERSION="EVENT_IMPULSE_INTRABAR_SHOCK_V1"
TIMEFRAMES=("1m","5m","1h")


def _num(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None


def _atr_value(value):
    if isinstance(value,dict):
        value=value.get("value")
    x=_num(value)
    return x if x is not None and x>0 else None


def quote_shock(last_closed_price, quote_price, atr_by_tf):
    """Compare the forming move with completed 1m/5m/1h volatility.

    GAME_CHANGER_EXTREME means the forming move already exceeds completed 1h
    ATR while also exceeding any available faster ATR. EVENT_IMPULSE means it
    exceeds completed 5m and 1m ATR but not 1h ATR.
    """
    base=_num(last_closed_price)
    px=_num(quote_price)
    out={"version":VERSION,"eligible":False,"severity":"NONE",
         "last_closed_price":base,"quote_price":px,"move_points":None,
         "ratios":{},"exceeds":{}}
    if base is None or px is None or base<=0 or px<=0:
        return out
    move=abs(px-base)
    ratios={}
    exceeds={}
    available=[]
    for tf in TIMEFRAMES:
        atr=_atr_value((atr_by_tf or {}).get(tf))
        if atr is None:
            continue
        available.append(tf)
        ratios[tf]=move/atr
        exceeds[tf]=move>=atr
    out.update(move_points=move,ratios=ratios,exceeds=exceeds,
               available_timeframes=available)
    if "1h" in exceeds and exceeds["1h"] and all(exceeds.get(tf,True) for tf in ("1m","5m")):
        out.update(eligible=True,severity="GAME_CHANGER_EXTREME",
                   reason="FORMING_MOVE_EXCEEDS_COMPLETED_1H_VOLATILITY")
    elif all(exceeds.get(tf,False) for tf in ("1m","5m")):
        out.update(eligible=True,severity="EVENT_IMPULSE",
                   reason="FORMING_MOVE_EXCEEDS_COMPLETED_FAST_VOLATILITY")
    else:
        out["reason"]="INTRABAR_VOLATILITY_SHOCK_NOT_CONFIRMED"
    return out
