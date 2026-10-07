"""Bar-boundary scheduling without parallel market-history object graphs.

The fast lane also refreshes hourly/four-hour decisions on their first cycle
after a close. A recently finished cycle does not make an older candle current.
This module schedules reads; it does not extend any entry or quote deadline.
"""
import math
import traceback

from veritas_timeframe_structure import timestamp

FAST = ("1m", "5m")
SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "4h": 14400}


def boundary(clock, horizon):
    stamp = timestamp(clock)
    return math.floor(stamp / SECONDS[horizon]) if stamp is not None else None


def can_reuse_fast(summary, assets, clock):
    """Reuse only a complete fast matrix containing the latest closed bars."""
    stamp = timestamp(clock)
    if stamp is None:
        return False
    rows = {(r.get("asset"), r.get("horizon")): r for r in summary or []}
    for asset in assets:
        for horizon in FAST:
            row = rows.get((asset, horizon), {})
            context = ((row.get("trade_plan") or {}).get("timeframe_entry_context")
                       or row.get("timeframe_entry_context") or {})
            closed = timestamp(context.get("closed_at"))
            expected = boundary(stamp, horizon) * SECONDS[horizon]
            if context.get("status") != "OK" or closed is None or not expected <= closed <= stamp:
                return False
    return bool(assets)


def fast_horizons(attempted, clock, available):
    due = [h for h in ("1h", "4h") if h in available
           and attempted.get(h) != boundary(clock, h)]
    return tuple(h for h in (*FAST, *due) if h in available)


def run(ns):
    """One serial loop; crossing an hourly close cannot wait for the full lane."""
    timer = ns["time"]
    start = timer.monotonic()
    next_fast, next_full = start, start + 5.0
    attempted = {h: boundary(timer.time(), h) for h in ("1h", "4h")}
    while True:
        tick, mode = timer.monotonic(), "IDLE"
        try:
            clock = timer.time()
            if tick >= next_full:
                mode, selected = "FULL", None
            elif tick >= next_fast:
                mode = "FAST_5M"
                selected = fast_horizons(attempted, clock, ns["HORIZONS"])
            else:
                timer.sleep(max(.5, min(5., min(next_fast, next_full) - tick)))
                continue
            ns["cycle"](selected, mode)
            # Record the START boundary: a close while the cycle was running
            # still needs its own refresh on the next fast pass.
            for h in ("1h", "4h"):
                if selected is None or h in selected:
                    attempted[h] = boundary(clock, h)
            if mode == "FULL":
                next_full = max(tick + ns["V90_FULL_CYCLE_INTERVAL_SECONDS"],
                                timer.monotonic() + ns["V90_FAST_5M_INTERVAL_SECONDS"])
                if next_fast <= tick:
                    next_fast = tick + ns["V90_FAST_5M_INTERVAL_SECONDS"]
            else:
                while next_fast <= tick:
                    next_fast += ns["V90_FAST_5M_INTERVAL_SECONDS"]
        except Exception as exc:
            error = {"status": "error", "at": ns["now"](), "version": ns["VERSION"],
                     "cycle_mode": mode, "error": f"{type(exc).__name__}: {exc}"}
            with ns["lock"]:
                if ns["last_cycle"].get("summary"):
                    ns["last_cycle"]["last_cycle_error"] = error
                else:
                    ns["last_cycle"].clear()
                    ns["last_cycle"].update(error)
                ns["last_cycle"]["cycle_in_progress"] = False
            ns["emit"]("cycle_error", cycle_mode=mode, error=error["error"],
                       trace=traceback.format_exc(limit=3))
            if mode == "FULL":
                next_full = timer.monotonic() + 30
            elif mode == "FAST_5M":
                next_fast = timer.monotonic() + 15
