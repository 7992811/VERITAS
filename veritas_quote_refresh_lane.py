"""Independent exact-source quote refresh lane for paper protection/evidence.

The lane has no book lock and no accounting authority. It reads only projected
open-position source identities, invokes the existing exact-source refresher,
and publishes results into the existing in-memory quote cache.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

VERSION="POSITION_QUOTE_REFRESH_LANE_V1"
INTERVAL_SECONDS=15.0
MAX_POSITIONS=64

_state={"status":"NOT_STARTED","version":VERSION,"interval_seconds":INTERVAL_SECONDS}
_lock=threading.Lock()

def snapshot():
    with _lock:
        return dict(_state)

def run_once(pg_connect, positions_sql, refresher, *, now=None):
    clock=now if now is not None else datetime.now(timezone.utc)
    started=time.monotonic()
    with pg_connect() as c:
        c.execute("SET LOCAL statement_timeout='2500ms'")
        rows=[dict(x) for x in c.execute(positions_sql+" ORDER BY asset,active_trade_id LIMIT %s",
                                         (MAX_POSITIONS,)).fetchall()]
    refresh_started=time.monotonic()
    result=refresher(rows) if rows else {}
    refresh_seconds=time.monotonic()-refresh_started
    groups=len(result or {}) if isinstance(result,dict) else 0
    return {"status":"OK","version":VERSION,"positions":len(rows),
            "refreshed_groups":groups,
            "read_seconds":round(refresh_started-started,4),
            "refresh_seconds":round(refresh_seconds,4),
            "duration_seconds":round(time.monotonic()-started,4),
            "checked_at":clock.isoformat() if hasattr(clock,"isoformat") else str(clock)}

def start(ns, positions_sql, refresher):
    with _lock:
        if _state.get("status") not in ("NOT_STARTED","ERROR"):
            return
        _state.update(status="STARTING",version=VERSION,interval_seconds=INTERVAL_SECONDS)

    def loop():
        last_log=0.0
        while True:
            started=time.monotonic()
            try:
                result=run_once(ns["pg_connect"],positions_sql,refresher)
                with _lock:_state.update(result)
                if time.monotonic()-last_log>=60:
                    ns["emit"]("position_quote_refresh_lane",**snapshot())
                    last_log=time.monotonic()
            except Exception as exc:
                with _lock:
                    _state.update(status="ERROR",error=f"{type(exc).__name__}: {exc}",
                                  checked_at=datetime.now(timezone.utc).isoformat())
                ns["emit"]("position_quote_refresh_lane_error",**snapshot())
            time.sleep(max(1.0,INTERVAL_SECONDS-(time.monotonic()-started)))

    threading.Thread(target=loop,daemon=True,name="veritas-position-quote-refresh").start()
