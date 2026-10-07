"""Read-only diagnostic boundary tests; no external or local network calls."""
from io import BytesIO
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import veritas_currency_automation_server as S


def isolated_env():
    return {"VERITAS_CURRENCY_AUTOMATION_STAGE": S.STAGE,
            "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}


class DiagnosticServerTests(unittest.TestCase):
    def test_requires_explicit_isolated_stage_before_probe_construction(self):
        probe = Mock()
        with self.assertRaisesRegex(S.DiagnosticConfigurationError, "ISOLATED_DIAGNOSTIC_STAGE_REQUIRED"):
            S.ReportState(None, env={}, probe=probe)
        probe.assert_not_called()

    def test_rejects_production_environment_and_every_execution_flag(self):
        env = isolated_env()
        env["VERITAS_CURRENCY_TRADE_ENVIRONMENT"] = "production"
        with self.assertRaisesRegex(S.DiagnosticConfigurationError, "SANDBOX_ENVIRONMENT_REQUIRED"):
            S.validate_stage(env)
        for flag in S.EXECUTION_FLAGS:
            with self.subTest(flag=flag):
                env = isolated_env()
                env[flag] = "true"
                with self.assertRaisesRegex(S.DiagnosticConfigurationError, "FLAGS_MUST_BE_OFF"):
                    S.validate_stage(env)

    def test_rejects_copied_real_broker_telegram_or_database_credentials(self):
        for name in ("TBANK_API_TOKEN", "TELEGRAM_BOT_TOKEN", "DATABASE_URL"):
            env = {**isolated_env(), name: "sensitive-fixture-never-output"}
            with self.assertRaises(S.DiagnosticConfigurationError) as error:
                S.validate_stage(env)
            self.assertNotIn("sensitive", str(error.exception))
            self.assertEqual(str(error.exception), "PRODUCTION_CREDENTIALS_NOT_ALLOWED_IN_DIAGNOSTICS")

    def test_accepts_only_bounded_nonreserved_port(self):
        self.assertEqual(S.validate_stage(isolated_env()), 10000)
        for port in ("0", "65536", "18012", "18013", "19099", "invalid"):
            with self.subTest(port=port), self.assertRaisesRegex(S.DiagnosticConfigurationError, "INVALID_DIAGNOSTIC_PORT"):
                S.validate_stage({**isolated_env(), "PORT": port})

    def test_evidence_read_is_bounded_and_corrupt_input_is_not_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "evidence.json"
            self.assertIsNone(S.read_evidence(p))
            for content in (b"{invalid", b"[]", b"x" * (S.MAX_EVIDENCE_BYTES + 1)):
                p.write_bytes(content)
                self.assertIsNone(S.read_evidence(p))
            p.write_text('{"status":"VERIFIED"}')
            self.assertEqual(S.read_evidence(p), {"status": "VERIFIED"})

    def test_refresh_pins_sandbox_and_http_snapshot_never_repeats_probe(self):
        env = {**isolated_env(), "TBANK_SANDBOX_TOKEN": "private-fixture"}
        probe = Mock(return_value={"status": "ACTION_REQUIRED"})
        builder = Mock(return_value={"status": "ACTION_REQUIRED", "scope": "DIAGNOSTICS"})
        state = S.ReportState({"counts": {"run": 18}}, env=env, probe=probe,
                              builder=builder, renderer=lambda _r: "<p>Проверка</p>")
        state.refresh()
        probe.assert_called_once_with(env, environment="sandbox", timeout=5.0)
        result, _html = state.snapshot()
        result["status"] = "FORGED"
        self.assertEqual(state.snapshot()[0]["status"], "ACTION_REQUIRED")
        self.assertEqual(probe.call_count, 1)
        self.assertNotIn("private-fixture", json.dumps(state.snapshot()))
        state.invalidate()
        self.assertEqual(state.snapshot(), (None, None))

    def test_offline_proof_is_renewed_without_secrets_or_repeat_within_twelve_hours(self):
        import veritas_currency_automation_report as R
        now = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
        evidence = {"status": "VERIFIED", "checked_at": now.isoformat(),
                    "source_fingerprint": R.source_fingerprint()}
        runner = Mock(return_value=evidence)
        state = S.ReportState(None, env=isolated_env(), probe=Mock(return_value={}),
                              builder=Mock(return_value={}), renderer=lambda _r: "<p>Проверка</p>",
                              offline_runner=runner, clock=lambda: now)
        state.refresh()
        runner.assert_called_once_with(timeout_seconds=60)
        state.refresh()
        self.assertEqual(runner.call_count, 1)
        state._clock = lambda: now + timedelta(hours=12)
        state.refresh()
        self.assertEqual(runner.call_count, 2)

    def test_expiry_is_rechecked_on_read_without_refreshing_broker_observation(self):
        current = [datetime(2026, 10, 8, 1, tzinfo=timezone.utc)]
        observed = current[0]
        probe = Mock(return_value={"checked_at": observed.isoformat()})
        def builder(**kwargs):
            age = (kwargs["now"] - observed).total_seconds()
            return {"status": "VERIFIED" if age < 900 else "NOT_CHECKED"}
        state = S.ReportState(None, env=isolated_env(), probe=probe, builder=builder,
                              renderer=lambda _r: "<p>Проверка</p>", clock=lambda: current[0])
        state.refresh()
        self.assertEqual(state.snapshot()[0]["status"], "VERIFIED")
        current[0] += timedelta(minutes=15)
        self.assertEqual(state.snapshot()[0]["status"], "NOT_CHECKED")
        probe.assert_called_once()

    def _handler(self, state, path="/report.json", method="GET"):
        handler = S.handler_for(state).__new__(S.handler_for(state))
        handler.path, handler.command = path, method
        handler.wfile = BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_post_has_no_action_or_refresh_path(self):
        state = Mock()
        handler = self._handler(state, "/api/v1/currency-trading/action", "POST")
        handler.do_POST()
        handler.send_response.assert_called_once_with(405)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"code": "READ_ONLY_DIAGNOSTIC"})
        state.snapshot.assert_not_called()
        state.refresh.assert_not_called()

    def test_health_proves_server_availability_even_when_broker_read_is_pending(self):
        state = Mock()
        state.snapshot.return_value = ({"status": "ACTION_REQUIRED"}, "<p>Не завершено</p>")
        handler = self._handler(state, "/healthz")
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        body = json.loads(handler.wfile.getvalue())
        self.assertTrue(body["report_available"])
        self.assertFalse(body["broker_order_submission_supported"])
        self.assertNotIn("readiness_verified", body)

    def test_http_does_not_serve_artifacts_or_credential_files(self):
        state = Mock()
        state.snapshot.return_value = ({"status": "ACTION_REQUIRED"}, "<p>Проверка</p>")
        for path in ("/.env", "/offline-evidence.json", "/../../etc/passwd", "/internal/currency-trading/poll"):
            with self.subTest(path=path):
                handler = self._handler(state, path)
                handler.do_GET()
                handler.send_response.assert_called_once_with(404)

    def test_read_headers_and_head_have_no_response_body(self):
        state = Mock()
        state.snapshot.return_value = ({"status": "NOT_CHECKED"}, "<p>Проверка</p>")
        handler = self._handler(state, "/report.json", "HEAD")
        handler.do_HEAD()
        self.assertEqual(handler.wfile.getvalue(), b"")
        self.assertIn(unittest.mock.call("Cache-Control", "no-store"), handler.send_header.call_args_list)
        self.assertIn(unittest.mock.call("X-Frame-Options", "DENY"), handler.send_header.call_args_list)


if __name__ == "__main__":
    unittest.main()
