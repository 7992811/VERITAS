"""Isolated read-only diagnostics; never imports the trading application or bot.

This server accepts no credentials or actions over HTTP. A sandbox read probe is
run at startup and on a bounded timer using only the service's sandbox settings.
Offline test evidence is generated at build time and revalidated against source.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit


STAGE = "sandbox_readiness"
EXECUTION_FLAGS = (
    "VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED",
    "VERITAS_LIVE_EXECUTION_ENABLED",
    "VERITAS_LIVE_EXECUTION_ARMED",
    "VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED",
    "VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED",
    "VERITAS_CURRENCY_BROKER_NOTIFICATIONS_ENABLED",
)
MAX_EVIDENCE_BYTES = 262144


class DiagnosticConfigurationError(ValueError):
    pass


def validate_stage(env):
    """Reject a copied production configuration before any network operation."""
    if env.get("VERITAS_CURRENCY_AUTOMATION_STAGE") != STAGE:
        raise DiagnosticConfigurationError("ISOLATED_DIAGNOSTIC_STAGE_REQUIRED")
    if env.get("VERITAS_CURRENCY_TRADE_ENVIRONMENT") != "sandbox":
        raise DiagnosticConfigurationError("SANDBOX_ENVIRONMENT_REQUIRED")
    if any(str(env.get(name, "")).strip().lower() not in ("", "0", "false", "no", "off")
           for name in EXECUTION_FLAGS):
        raise DiagnosticConfigurationError("DIAGNOSTIC_EXECUTION_FLAGS_MUST_BE_OFF")
    if env.get("TBANK_API_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or env.get("DATABASE_URL"):
        raise DiagnosticConfigurationError("PRODUCTION_CREDENTIALS_NOT_ALLOWED_IN_DIAGNOSTICS")
    try:
        port = int(env.get("PORT", "10000"))
    except (TypeError, ValueError):
        raise DiagnosticConfigurationError("INVALID_DIAGNOSTIC_PORT") from None
    if not 1 <= port <= 65535 or port in (18012, 18013, 19099):
        raise DiagnosticConfigurationError("INVALID_DIAGNOSTIC_PORT")
    return port


def read_evidence(path):
    """Local, bounded, caller-selected artifact. No paths come from HTTP."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


class ReportState:
    def __init__(self, offline_evidence, *, env, probe=None, builder=None,
                 renderer=None, telegram_observation=None, offline_runner=None,
                 clock=None):
        # Configuration is fixed for this process; changing secrets requires a
        # normal service restart and revalidation, never an HTTP request.
        validate_stage(env)
        if probe is None:
            from veritas_currency_sandbox_probe import run_probe
            probe = run_probe
        if builder is None or renderer is None:
            from veritas_currency_automation_report import build_report, render_html
            builder, renderer = builder or build_report, renderer or render_html
        self._env = dict(env)
        self._probe, self._builder, self._renderer = probe, builder, renderer
        self._offline_runner = offline_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._offline = deepcopy(offline_evidence)
        self._telegram = deepcopy(telegram_observation)
        self._lock = threading.Lock()
        self._report = None
        self._html = None
        self._sandbox = None

    def refresh(self):
        if self._offline_runner is not None:
            try:
                stamp = datetime.fromisoformat(str((self._offline or {}).get("checked_at")).replace("Z", "+00:00"))
                age = (self._clock() - stamp).total_seconds() if stamp.tzinfo else -1
            except (TypeError, ValueError):
                age = -1
            from veritas_currency_automation_report import source_fingerprint
            if (not 0 <= age < 43200 or (self._offline or {}).get("status") != "VERIFIED"
                    or (self._offline or {}).get("source_fingerprint") != source_fingerprint()):
                # This helper runs only the offline allowlist in a child process
                # with all network connections disabled and no inherited secrets.
                self._offline = self._offline_runner(timeout_seconds=60)
        # run_probe has a fixed sandbox-only read allowlist, bounded requests,
        # and no live token fallback. HTTP clients cannot trigger this method.
        sandbox = self._probe(self._env, environment="sandbox", timeout=5.0)
        report = self._builder(offline_evidence=self._offline, sandbox_report=sandbox,
                               telegram_observation=self._telegram, now=self._clock())
        html = self._renderer(report)
        with self._lock:
            self._report, self._html, self._sandbox = deepcopy(report), html, deepcopy(sandbox)
        return report

    def snapshot(self):
        with self._lock:
            if self._report is not None:
                try:
                    # Re-evaluate deadlines on every read using cached evidence.
                    # This does not refresh broker/Telegram observations.
                    self._report = self._builder(offline_evidence=self._offline,
                        sandbox_report=self._sandbox, telegram_observation=self._telegram,
                        now=self._clock())
                    self._html = self._renderer(self._report)
                except Exception:
                    self._report, self._html = None, None
            return deepcopy(self._report), self._html

    def invalidate(self):
        # A failed refresh must not keep displaying a stale successful proof.
        with self._lock:
            self._report, self._html = None, None


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VERITAS-Diagnostics"
        sys_version = ""

        def log_message(self, *_args):
            # URLs may contain user-supplied text; keep them out of app logs.
            pass

        def _respond(self, value, status=200, *, html=False):
            body = value.encode("utf-8") if html else json.dumps(
                value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            report, html = state.snapshot()
            path = urlsplit(self.path).path
            if path not in ("/", "/report.json", "/healthz"):
                self._respond({"code": "NOT_FOUND"}, 404)
            elif report is None:
                self._respond({"code": "DIAGNOSTIC_STARTING"}, 503)
            elif path == "/healthz":
                self._respond({"ok": True, "service": "currency_diagnostics",
                               "stage": STAGE, "report_available": True,
                               "broker_order_submission_supported": False})
            elif path == "/report.json":
                self._respond(report)
            else:
                self._respond(html, html=True)

        do_HEAD = do_GET

        def _deny_action(self):
            self.close_connection = True
            self._respond({"code": "READ_ONLY_DIAGNOSTIC"}, 405)

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _deny_action

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve isolated Currency diagnostics.")
    parser.add_argument("--evidence-file", default="currency_automation_output/offline-evidence.json")
    parser.add_argument("--telegram-observation")
    args = parser.parse_args(argv)
    try:
        port = validate_stage(os.environ)
    except DiagnosticConfigurationError as error:
        print(json.dumps({"event": "currency_diagnostics_configuration", "code": str(error)}), flush=True)
        return 2
    from tools.run_currency_automation_report import run_offline_checks
    state = ReportState(read_evidence(args.evidence_file), env=os.environ,
                        telegram_observation=read_evidence(args.telegram_observation)
                        if args.telegram_observation else None,
                        offline_runner=run_offline_checks)
    stop = threading.Event()

    def refresh():
        try:
            report = state.refresh()
            print(json.dumps({"event": "currency_automation_report", "report": report},
                             ensure_ascii=False, allow_nan=False), flush=True)
        except Exception:
            # No broker exception, environment value or credential is logged.
            state.invalidate()
            print(json.dumps({"event": "currency_automation_report", "code": "DIAGNOSTIC_REFRESH_FAILED"}), flush=True)

    refresh()
    def periodic():
        while not stop.wait(300):
            refresh()
    threading.Thread(target=periodic, name="sandbox-read-diagnostics", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_for(state))
    print(json.dumps({"event": "currency_diagnostics_ready", "stage": STAGE, "port": port,
                      "only_read_endpoints": True, "real_trading_supported": False}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
