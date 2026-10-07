#!/usr/bin/env python3
"""Run bounded offline checks and write sanitized Currency diagnostics."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import veritas_currency_automation_report as report


CHILD = r'''
import io, json, socket, sys, unittest
def no_network(*args, **kwargs):
    raise OSError("OFFLINE_TEST_NETWORK_DISABLED")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
suite = unittest.defaultTestLoader.loadTestsFromNames(json.loads(sys.argv[1]))
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
counts = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
              skipped=len(result.skipped), expected_failures=len(result.expectedFailures),
              unexpected_successes=len(result.unexpectedSuccesses))
counts["passed"] = counts["run"] - sum(value for key, value in counts.items() if key != "run")
status = "FAILED" if not result.wasSuccessful() else "ACTION_REQUIRED" if counts["skipped"] or counts["expected_failures"] else "VERIFIED"
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(dict(status=status, counts=counts), handle)
'''


def run_offline_checks(*, modules=None, timeout_seconds=60):
    """Only the explicit offline allowlist, with no inherited integration secrets."""
    modules = tuple(modules) if modules is not None else report.REQUIRED_TEST_MODULES
    if not modules or any(module not in report.REQUIRED_TEST_MODULES for module in modules) or len(set(modules)) != len(modules):
        raise ValueError("Only distinct documented offline test modules are allowed")
    if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 60:
        raise ValueError("Offline timeout must be between 1 and 60 seconds")
    fingerprint = report.source_fingerprint()
    result = {"status": "NOT_CHECKED", "counts": None}
    with tempfile.TemporaryDirectory(prefix="veritas-currency-check-") as temporary:
        evidence_path = Path(temporary) / "result.json"
        try:
            completed = subprocess.run([sys.executable, "-c", CHILD, json.dumps(modules), str(evidence_path)],
                                       cwd=ROOT, env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
                                       timeout=timeout_seconds, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if completed.returncode == 0 and evidence_path.is_file() and evidence_path.stat().st_size <= 32768:
                result = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    if fingerprint != report.source_fingerprint():
        result = {"status": "NOT_CHECKED", "counts": result.get("counts")}
    return {"status": result.get("status", "NOT_CHECKED"), "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "local_unittest", "source_fingerprint": fingerprint, "modules": list(modules), "counts": result.get("counts")}


def read_json(path):
    if path is None:
        return None
    path = Path(path)
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Input observation exceeds the size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Input observation must be a JSON object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create read-only Currency diagnostics. No broker or Telegram requests.")
    parser.add_argument("--output-dir", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--offline-evidence")
    source.add_argument("--run-offline", action="store_true")
    parser.add_argument("--sandbox-report")
    parser.add_argument("--config-observation")
    parser.add_argument("--telegram-observation")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        evidence = read_json(args.offline_evidence) if args.offline_evidence else run_offline_checks(timeout_seconds=args.timeout)
        public = report.build_report(offline_evidence=evidence, sandbox_report=read_json(args.sandbox_report),
                                     config_observation=read_json(args.config_observation), telegram_observation=read_json(args.telegram_observation))
        directory = Path(args.output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (directory / "index.html").write_text(report.render_html(public), encoding="utf-8")
        # Retain only the exact evidence schema; a supplied raw file is not copied.
        clean = {key: evidence.get(key) for key in ("status", "checked_at", "source", "source_fingerprint", "modules", "counts")}
        if args.offline_evidence is None:
            (directory / "offline-evidence.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": public["status"], "offline_status": public["checks"][0]["status"],
                          "sandbox_read_access_verified": public["sandbox_read_access_verified"],
                          "execution_permission_granted": False}, ensure_ascii=False))
        return 0 if public["checks"][0]["status"] == "VERIFIED" else 1
    except (OSError, ValueError, TypeError, KeyError):
        print('{"status":"NOT_CHECKED","error":"DIAGNOSTIC_REPORT_NOT_WRITTEN"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
