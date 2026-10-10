"""Optional embedded VERITAS signal-robot bootstrap.

This module is intentionally inert unless explicitly enabled and only activates
for the production web entrypoint. It exists so the Render web service can host
the thin signal-poll loop without a second Background Worker.

It never changes execution/autotrade gates. The Currency service remains the
sole authority for broker mutations.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import sys
# Import these synchronously before any bootstrap thread exists. Python 3.14
# can otherwise expose partially initialized typing/dataclasses modules when
# sitecustomize starts a worker while the main entrypoint imports traceback.
import typing as _typing
import dataclasses as _dataclasses
import traceback as _traceback
import threading
import time

_ENABLED = {"1","true","yes","on"}
_stop = threading.Event()
_started = False
_start_requested = False
_lock = threading.Lock()


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _ENABLED


def should_start() -> bool:
    if not _enabled("VERITAS_SIGNAL_ROBOT_EMBEDDED"):
        return False
    if Path(sys.argv[0]).name != "veritas_intelligence.py":
        return False
    return _enabled("VERITAS_SIGNAL_ROBOT_ENABLED")


def _emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields},
                     ensure_ascii=False, separators=(",", ":")), flush=True)


def _bootstrap() -> None:
    global _started
    # Give the web process a brief head start. Failure is non-fatal; the robot
    # itself retries transient signal/service errors.
    _stop.wait(3.0)
    if _stop.is_set():
        return
    try:
        from veritas_signal_robot import start_from_env
        result = start_from_env(_stop, log=print)
        if result is None:
            _emit("embedded_signal_robot", status="DISABLED")
            return
        with _lock:
            _started = True
        _emit("embedded_signal_robot", status="STARTED")
        while not _stop.wait(1.0):
            pass
        robot, thread = result
        close = getattr(robot, "close", None)
        if callable(close):
            close()
        if thread.is_alive():
            thread.join(timeout=2.0)
    except Exception as exc:
        code = str(exc)
        if not code or len(code) > 80 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in code):
            code = "EMBEDDED_SIGNAL_ROBOT_BOOTSTRAP_FAILED"
        _emit("embedded_signal_robot", status="FAILED", code=code)


def start() -> bool:
    global _start_requested
    if not should_start():
        return False
    with _lock:
        if _start_requested:
            return True
        _start_requested = True
    thread = threading.Thread(target=_bootstrap,
                              daemon=True,
                              name="veritas-embedded-signal-robot-bootstrap")
    thread.start()
    return True


def stop() -> None:
    _stop.set()


atexit.register(stop)
start()
