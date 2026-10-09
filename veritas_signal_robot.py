"""VERITAS Currency signal robot.

The robot is deliberately thin: VERITAS remains the signal authority and the
existing Currency trade service remains the execution/risk authority.

Sandbox may be configured for autonomous proposal approval/execution. Production
never auto-approves here: the internal trade service keeps signed owner approval
as the final mutation gate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

import httpx


SIGNALS_PATH = "/api/v1/signals"
TRADE_POLL_PATH = "/internal/currency-trading/poll"
EXPECTED_HOST = "veritas-intelligence-v1.onrender.com"
ASSET = "CNYRUBF"
DIRECTIONS = frozenset(("LONG", "SHORT"))
HORIZON_RANK = {"1m": 7, "5m": 6, "1h": 5, "4h": 4, "1d": 3, "3d": 2, "7d": 1}


class SignalRobotError(RuntimeError):
    pass


def _origin(value):
    parsed = urlparse(str(value or ""))
    try:
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == EXPECTED_HOST
            and parsed.port in (None, 443)
            and parsed.path in ("", "/")
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise SignalRobotError("INVALID_VERITAS_SIGNAL_ORIGIN")
    return str(value).rstrip("/")


def _positive_id(value):
    raw = str(value or "")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        raise SignalRobotError("EXPLICIT_TRADE_BOT_ID_REQUIRED")
    return int(raw)


def _seconds(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 2.0
    return min(5.0, max(1.0, parsed))


def _event(row):
    if not isinstance(row, dict):
        return {}
    context = row.get("timeframe_entry_context") or row.get("trend_entry_context") or {}
    event = context.get("event") if isinstance(context, dict) else {}
    return event if isinstance(event, dict) else {}


def _candidate_key(row):
    event = _event(row)
    try:
        signal_at = float(event.get("signal_at") or 0.0)
    except (TypeError, ValueError):
        signal_at = 0.0
    return signal_at, HORIZON_RANK.get(str(row.get("horizon") or ""), 0), str(event.get("event_id") or "")


def select_currency_signal(payload):
    """Choose the newest verified directional CNYRUBF event for robot telemetry.

    This selection does not authorize a trade. The internal Currency service
    independently repeats canonical entry selection against fresh broker facts.
    """
    rows = payload.get("signals") if isinstance(payload, dict) else None
    eligible = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("asset") != ASSET:
            continue
        direction = row.get("research_decision") or row.get("decision")
        event = _event(row)
        if (direction not in DIRECTIONS or event.get("direction") != direction
                or not event.get("event_id") or row.get("source_gate_pass") is not True
                or row.get("snapshot_stale") is True):
            continue
        eligible.append(row)
    return max(eligible, key=_candidate_key) if eligible else None


class VeritasSignalRobot:
    def __init__(self, base_url, service_key, bot_id, *, environment="sandbox",
                 client=None, log=print):
        self.base_url = _origin(base_url)
        if not isinstance(service_key, str) or len(service_key.encode()) < 32:
            raise SignalRobotError("TRADE_SERVICE_KEY_NOT_CONFIGURED")
        if environment not in ("sandbox", "production"):
            raise SignalRobotError("INVALID_EXECUTION_ENVIRONMENT")
        self.service_key = service_key
        self.bot_id = _positive_id(bot_id)
        self.environment = environment
        self.http = client or httpx.Client(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False, trust_env=False)
        self.log = log
        self.last_event_id = None

    def close(self):
        close = getattr(self.http, "close", None)
        if callable(close):
            close()

    def emit(self, event, **fields):
        self.log(json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":")))

    def _get_signals(self):
        try:
            response = self.http.get(self.base_url + SIGNALS_PATH)
        except Exception:
            raise SignalRobotError("VERITAS_SIGNAL_SOURCE_UNAVAILABLE") from None
        if response.status_code != 200:
            raise SignalRobotError("VERITAS_SIGNAL_SOURCE_UNAVAILABLE")
        try:
            payload = response.json()
        except Exception:
            raise SignalRobotError("INVALID_VERITAS_SIGNAL_RESPONSE") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("signals"), list):
            raise SignalRobotError("INVALID_VERITAS_SIGNAL_RESPONSE")
        return payload

    def _trade_poll(self):
        try:
            response = self.http.post(
                self.base_url + TRADE_POLL_PATH,
                headers={"X-Veritas-Trade-Key": self.service_key},
                json={"bot_id": self.bot_id},
            )
        except Exception:
            raise SignalRobotError("TRADE_SERVICE_UNAVAILABLE") from None
        try:
            payload = response.json()
        except Exception:
            raise SignalRobotError("INVALID_TRADE_SERVICE_RESPONSE") from None
        if not isinstance(payload, dict):
            raise SignalRobotError("INVALID_TRADE_SERVICE_RESPONSE")
        if response.status_code >= 500:
            raise SignalRobotError(str(payload.get("code") or "TRADE_SERVICE_UNAVAILABLE"))
        if response.status_code >= 400:
            raise SignalRobotError(str(payload.get("code") or "TRADE_REQUEST_REJECTED"))
        # A production worker may never accept a server claiming autonomous
        # sandbox execution. Fail closed rather than guessing configuration.
        if self.environment == "production" and payload.get("sandbox_autotrade_enabled") is True:
            raise SignalRobotError("SANDBOX_AUTOTRADE_PRODUCTION_FORBIDDEN")
        return payload

    def tick(self):
        signals = self._get_signals()
        selected = select_currency_signal(signals)
        event = _event(selected) if selected else {}
        event_id = event.get("event_id")
        result = self._trade_poll()
        changed = bool(event_id and event_id != self.last_event_id)
        if event_id:
            self.last_event_id = str(event_id)
        self.emit(
            "veritas_signal_robot_tick",
            status="OK",
            environment=self.environment,
            asset=ASSET,
            signal_event_id=event_id,
            signal_direction=(selected or {}).get("research_decision") or (selected or {}).get("decision"),
            signal_horizon=(selected or {}).get("horizon"),
            signal_changed=changed,
            trade_block_reason=result.get("block_reason"),
            execution_enabled=result.get("execution_enabled") is True,
            sandbox_autotrade_enabled=result.get("sandbox_autotrade_enabled") is True,
            pending_items=len(result.get("items") or []),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"signal": selected, "trade": result, "signal_changed": changed}

    def run(self, stop_event, *, interval_seconds=2.0):
        interval = _seconds(interval_seconds)
        last_error = None
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
                last_error = None
            except SignalRobotError as exc:
                code = str(exc)
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                    code = "SIGNAL_ROBOT_ERROR"
                if code != last_error:
                    self.emit("veritas_signal_robot_error", code=code,
                              environment=self.environment)
                    last_error = code
            stop_event.wait(max(0.0, interval - (time.monotonic() - started)))


def build_from_env(*, client=None, log=print):
    if os.getenv("VERITAS_SIGNAL_ROBOT_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    return VeritasSignalRobot(
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_URL",
                  "https://veritas-intelligence-v1.onrender.com"),
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_KEY", ""),
        os.getenv("VERITAS_CURRENCY_TRADE_BOT_ID", ""),
        environment=os.getenv("VERITAS_CURRENCY_TRADE_ENVIRONMENT", "sandbox"),
        client=client, log=log,
    )


def start_from_env(stop_event, *, client=None, log=print):
    robot = build_from_env(client=client, log=log)
    if robot is None:
        return None
    interval = _seconds(os.getenv("VERITAS_SIGNAL_ROBOT_INTERVAL_SECONDS", "2"))
    thread = threading.Thread(
        target=robot.run, args=(stop_event,), kwargs={"interval_seconds": interval},
        daemon=True, name="veritas-signal-robot")
    thread.start()
    return robot, thread


if __name__ == "__main__":
    stop = threading.Event()
    robot = build_from_env()
    if robot is None:
        raise SystemExit("VERITAS_SIGNAL_ROBOT_ENABLED is not enabled")
    try:
        robot.run(stop, interval_seconds=_seconds(
            os.getenv("VERITAS_SIGNAL_ROBOT_INTERVAL_SECONDS", "2")))
    except KeyboardInterrupt:
        stop.set()
    finally:
        robot.close()
