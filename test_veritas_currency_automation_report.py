"""Public diagnostics cannot turn missing or offline evidence into live readiness."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_currency_automation_report as R
from tools import run_currency_automation_report as CLI

NOW = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def evidence(**changes):
    value = {"status": "VERIFIED", "checked_at": NOW.isoformat(), "source": "local_unittest",
             "source_fingerprint": FINGERPRINT, "modules": list(R.REQUIRED_TEST_MODULES),
             "counts": dict(run=20, passed=20, failures=0, errors=0, skipped=0, expected_failures=0, unexpected_successes=0)}
    value.update(changes)
    return value


def sandbox(**changes):
    value = {"version": R.SANDBOX_PROBE_VERSION, "status": "VERIFIED", "checked_at": NOW.isoformat(), "environment": "sandbox",
             "read_access_verified": True, "live_execution_verified": False, "order_submission_tested": False,
             "observed_config": {"sandbox_token_present": True, "explicit_sandbox_account_present": True, "environment_is_sandbox": True},
             "checks": [{"code": code, "status": "VERIFIED", "checked_at": NOW.isoformat()} for code in sorted(R.SANDBOX_REQUIRED_CHECKS)]}
    value.update(changes)
    return value


def telegram(**changes):
    value = {"status": "VERIFIED", "checked_at": NOW.isoformat(), "bot_identity_verified": True,
             "channel_identity_verified": True, "can_post_messages": True}
    value.update(changes)
    return value


def build(**values):
    values.setdefault("now", NOW)
    values.setdefault("expected_source_fingerprint", FINGERPRINT)
    return R.build_report(**values)


def check(report, code):
    return next(row for row in report["checks"] if row["code"] == code)


class EvidenceSemanticsTests(unittest.TestCase):
    def test_offline_pass_does_not_verify_connection_or_grant_execution(self):
        result = build(offline_evidence=evidence())
        self.assertEqual(check(result, "offline_tests")["status"], "VERIFIED")
        self.assertEqual(check(result, "cny_potential_filter")["observed_multiple"], 1.1)
        self.assertFalse(result["sandbox_read_access_verified"])
        self.assertFalse(result["execution_permission_granted"])
        self.assertEqual(result["real_execution_readiness"], "NOT_CHECKED")
        self.assertEqual(result["status"], "NOT_CHECKED")
        self.assertEqual(result["owner_action_codes"], [])

    def test_skips_and_expected_failures_cannot_be_counted_as_passes(self):
        for category in ("skipped", "expected_failures"):
            with self.subTest(category=category):
                proof = evidence()
                proof["counts"][category] = 1
                proof["counts"]["passed"] = 19
                result = check(build(offline_evidence=proof), "offline_tests")
                self.assertEqual(result["status"], "ACTION_REQUIRED")
                self.assertEqual(result["counts"]["passed"], 19)

    def test_failures_errors_and_unexpected_success_are_failures(self):
        for category in ("failures", "errors", "unexpected_successes"):
            with self.subTest(category=category):
                proof = evidence()
                proof["counts"][category], proof["counts"]["passed"] = 1, 19
                self.assertEqual(check(build(offline_evidence=proof), "offline_tests")["status"], "FAILED")

    def test_malformed_inconsistent_zero_and_missing_counts_never_pass(self):
        for counts in ({"run": 20, "passed": 20}, None, {**evidence()["counts"], "run": 21},
                       {**evidence()["counts"], "run": 0, "passed": 0},
                       {**evidence()["counts"], "errors": True},
                       {**evidence()["counts"], "passed": "20"}):
            with self.subTest(counts=counts):
                self.assertEqual(check(build(offline_evidence=evidence(counts=counts)), "offline_tests")["status"], "NOT_CHECKED")

    def test_missing_module_or_unknown_source_cannot_claim_full_selected_coverage(self):
        result = check(build(offline_evidence=evidence(modules=list(R.REQUIRED_TEST_MODULES)[:-1])), "offline_tests")
        self.assertEqual((result["status"], result["reason"]), ("ACTION_REQUIRED", "OFFLINE_INCOMPLETE"))
        for source in (None, "arbitrary-label", {"token": "private"}):
            self.assertEqual(check(build(offline_evidence=evidence(source=source)), "offline_tests")["status"], "NOT_CHECKED")

    def test_stale_future_naive_and_source_mismatched_evidence_is_invalidated(self):
        for timestamp, reason in (((NOW - timedelta(days=1)).isoformat(), "EVIDENCE_STALE"),
                                  ((NOW + timedelta(seconds=31)).isoformat(), "EVIDENCE_INVALID"),
                                  ("2026-10-07T20:00:00", "EVIDENCE_INVALID")):
            result = check(build(offline_evidence=evidence(checked_at=timestamp)), "offline_tests")
            self.assertEqual((result["status"], result["reason"]), ("NOT_CHECKED", reason))
        result = check(build(offline_evidence=evidence(source_fingerprint="b" * 64)), "offline_tests")
        self.assertEqual((result["status"], result["reason"]), ("NOT_CHECKED", "SOURCE_CHANGED"))

    def test_canonical_rule_read_is_not_an_assumed_constant(self):
        with patch("veritas_costs.policy", return_value={"entry_cost_multiple": 2.0}):
            result = check(build(), "cny_potential_filter")
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["observed_multiple"], 2.0)
        with patch("veritas_costs.policy", return_value={"entry_cost_multiple": float("nan")}):
            result = check(build(), "cny_potential_filter")
            self.assertEqual(result["status"], "FAILED")
            self.assertIsNone(result["observed_multiple"])

    def test_sandbox_success_is_only_fresh_read_access(self):
        result = build(offline_evidence=evidence(), sandbox_report=sandbox(), telegram_observation=telegram(owner_binding_verified=True))
        self.assertTrue(result["sandbox_read_access_verified"])
        self.assertEqual(check(result, "sandbox_order_flow")["status"], "NOT_CHECKED")
        self.assertEqual(result["real_execution_readiness"], "NOT_CHECKED")
        self.assertFalse(result["execution_permission_granted"])
        self.assertEqual(result["status"], "NOT_CHECKED")

    def test_wrong_scope_or_claimed_order_submission_rejects_read_only_probe(self):
        for change in ({"environment": "production"}, {"order_submission_tested": True},
                       {"live_execution_verified": True}, {"live_execution_verified": "false"}):
            result = build(sandbox_report=sandbox(**change))
            self.assertFalse(result["sandbox_read_access_verified"])
            self.assertEqual(check(result, "sandbox_read_access")["status"], "FAILED")

    def test_fresh_summary_cannot_hide_stale_missing_or_failed_probe_steps(self):
        for steps in ([], None, [{"status": "VERIFIED"}],
                      [{"status": "VERIFIED", "checked_at": (NOW - timedelta(minutes=15)).isoformat()}],
                      [{"status": "FAILED", "checked_at": NOW.isoformat()}]):
            result = build(sandbox_report=sandbox(checks=steps))
            self.assertFalse(result["sandbox_read_access_verified"])

    def test_sandbox_cannot_pass_with_partial_arbitrary_or_duplicate_checks(self):
        valid = sandbox()["checks"]
        for steps in (valid[:-1], valid + [valid[0]], [valid[0]] * len(valid),
                      [{**item, "code": "arbitrary" + str(index)} for index, item in enumerate(valid)]):
            result = build(sandbox_report=sandbox(checks=steps))
            self.assertEqual(check(result, "sandbox_read_access")["status"], "NOT_CHECKED")
            self.assertFalse(result["sandbox_read_access_verified"])
        for version in (None, "arbitrary", "CURRENCY_SANDBOX_READ_PROBE_V2"):
            result = build(sandbox_report=sandbox(version=version))
            self.assertEqual(check(result, "sandbox_read_access")["status"], "NOT_CHECKED")

    def test_unknown_token_is_not_reported_as_missing(self):
        for config in (None, {}, {"checked_at": NOW.isoformat()},
                       {"checked_at": NOW.isoformat(), "sandbox_token_present": "false"}):
            result = build(config_observation=config)
            self.assertEqual(check(result, "sandbox_read_access")["status"], "NOT_CHECKED")
            self.assertNotIn("SANDBOX_TOKEN_REQUIRED", result["owner_action_codes"])
            self.assertNotIn(R.TEXT["SANDBOX_TOKEN_REQUIRED"], R.render_html(result))

    def test_only_fresh_explicit_absence_creates_owner_action(self):
        config = {"checked_at": NOW.isoformat(), "sandbox_token_present": False}
        result = build(config_observation=config)
        self.assertEqual(result["owner_action_codes"], ["SANDBOX_TOKEN_REQUIRED"])
        stale = build(config_observation=config, now=NOW + timedelta(minutes=15))
        self.assertEqual(stale["owner_action_codes"], [])
        self.assertEqual(check(stale, "sandbox_read_access")["status"], "NOT_CHECKED")

    def test_actual_telegram_rights_remain_verified_when_owner_binding_unknown(self):
        result = build(telegram_observation=telegram())
        self.assertEqual(check(result, "telegram_identity")["status"], "VERIFIED")
        self.assertEqual(check(result, "telegram_owner_binding")["status"], "NOT_CHECKED")
        self.assertEqual(result["owner_action_codes"], [])

    def test_telegram_identity_permissions_and_owner_observations_remain_distinct(self):
        result = build(telegram_observation=telegram(owner_binding_verified=False))
        self.assertEqual(check(result, "telegram_identity")["status"], "VERIFIED")
        self.assertIn("TELEGRAM_OWNER_BINDING_REQUIRED", result["owner_action_codes"])
        result = build(telegram_observation=telegram(can_post_messages=False))
        self.assertIn("TELEGRAM_POST_PERMISSION_REQUIRED", result["owner_action_codes"])
        result = build(telegram_observation=telegram(bot_identity_verified=False, owner_binding_verified=True))
        self.assertEqual(check(result, "telegram_identity")["status"], "FAILED")
        self.assertEqual(check(result, "telegram_owner_binding")["status"], "NOT_CHECKED")

    def test_expired_telegram_proof_does_not_preserve_success_or_owner_action(self):
        for binding in (False, True):
            result = build(telegram_observation=telegram(owner_binding_verified=binding), now=NOW + timedelta(minutes=15))
            self.assertEqual(check(result, "telegram_identity")["status"], "NOT_CHECKED")
            self.assertEqual(check(result, "telegram_owner_binding")["status"], "NOT_CHECKED")
            self.assertEqual(result["owner_action_codes"], [])

    def test_report_strips_private_nested_fields_raw_errors_and_identifiers(self):
        secret = "private-token-account-balance-should-not-escape"
        proof, probe, link = evidence(), sandbox(), telegram()
        for raw in (proof, probe, link):
            raw.update(token=secret, account_id=secret, balance=secret, detail=secret, error=secret)
        probe["checks"][0].update(detail=secret, account_id=secret, code=secret)
        probe["observed_config"].update(raw_token=secret, account_id=secret)
        result = build(offline_evidence=proof, sandbox_report=probe, telegram_observation=link)
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, R.render_html(result))

    def test_malformed_statuses_do_not_escape_or_crash_public_report(self):
        self.assertEqual(check(build(offline_evidence=evidence(status={"secret": "value"})), "offline_tests")["status"], "NOT_CHECKED")
        html = R.render_html({"generated_at": "<script>private</script>", "checks": [
            {"code": {"secret": "value"}}, {"code": "offline_tests", "status": [], "reason": {"token": "private"}, "counts": []}],
            "owner_action_codes": [{"private": "value"}, "<script>private</script>"]})
        self.assertNotIn("private", html)
        self.assertNotIn("<script>private</script>", html)

    def test_html_is_standalone_mobile_read_only_and_expiring(self):
        html = R.render_html(build(offline_evidence=evidence(), sandbox_report=sandbox()))
        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))
        parser = Tags()
        parser.feed(html)
        self.assertIn(("html", {"lang": "ru"}), parser.tags)
        self.assertTrue(any(tag == "meta" and attrs.get("name") == "viewport" for tag, attrs in parser.tags))
        self.assertFalse(any(tag in {"form", "script"} for tag, attrs in parser.tags))
        self.assertEqual(sum(tag == "section" and attrs.get("class") == "card" for tag, attrs in parser.tags), 6)
        self.assertIn("data-valid-until=", html)
        self.assertIn(("meta", {"http-equiv": "refresh", "content": "60"}), parser.tags)
        self.assertIn("Действует до:", html)
        for forbidden in ("localStorage", "fetch(", "TBANK_", "TELEGRAM_BOT_TOKEN", "account_id", "balance"):
            self.assertNotIn(forbidden, html)


class OfflineRunnerTests(unittest.TestCase):
    def test_runner_has_fixed_allowlist_and_bounded_timeout(self):
        for modules in (("os",), ("test_veritas_currency_trade_ui;rm",), (), (R.REQUIRED_TEST_MODULES[0],) * 2):
            with self.assertRaises(ValueError):
                CLI.run_offline_checks(modules=modules)
        for timeout in (0, 61, True, float("inf")):
            with self.assertRaises(ValueError):
                CLI.run_offline_checks(timeout_seconds=timeout)

    def test_runner_does_not_inherit_credentials_or_publish_raw_child_output(self):
        captured = []
        def child(command, **kwargs):
            captured.append(kwargs)
            Path(command[-1]).write_text(json.dumps({"status": "VERIFIED", "counts": evidence()["counts"]}))
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"TBANK_API_TOKEN": "private", "TELEGRAM_BOT_TOKEN": "private"}), \
                patch.object(CLI.subprocess, "run", side_effect=child), patch.object(R, "source_fingerprint", return_value=FINGERPRINT):
            result = CLI.run_offline_checks()
        self.assertEqual(result["status"], "VERIFIED")
        self.assertNotIn("TBANK_API_TOKEN", captured[0]["env"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", captured[0]["env"])
        self.assertEqual(captured[0]["stdout"], subprocess.DEVNULL)
        self.assertEqual(captured[0]["stderr"], subprocess.DEVNULL)
        self.assertNotIn("private", json.dumps(result))

    def test_timeout_does_not_become_pass_and_does_not_publish_exception(self):
        with patch.object(CLI.subprocess, "run", side_effect=subprocess.TimeoutExpired("private-command", 2)), \
                patch.object(R, "source_fingerprint", return_value=FINGERPRINT):
            result = CLI.run_offline_checks(timeout_seconds=2)
        self.assertEqual(result["status"], "NOT_CHECKED")
        self.assertIsNone(result["counts"])
        self.assertNotIn("private-command", json.dumps(result))

    def test_changed_source_during_checks_invalidates_result(self):
        def child(command, **_):
            Path(command[-1]).write_text(json.dumps({"status": "VERIFIED", "counts": evidence()["counts"]}))
            return SimpleNamespace(returncode=0)
        with patch.object(CLI.subprocess, "run", side_effect=child), \
                patch.object(R, "source_fingerprint", side_effect=[FINGERPRINT, "b" * 64]):
            self.assertEqual(CLI.run_offline_checks()["status"], "NOT_CHECKED")

    def test_actual_isolated_child_records_real_counts_for_selected_ui_module(self):
        result = CLI.run_offline_checks(modules=["test_veritas_currency_trade_ui"], timeout_seconds=20)
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertEqual(result["counts"]["run"], 3)
        self.assertEqual(result["counts"]["passed"], 3)
        self.assertEqual(result["counts"]["skipped"], 0)
        public = R.build_report(offline_evidence=result)
        self.assertEqual(check(public, "offline_tests")["status"], "ACTION_REQUIRED")

    def test_cli_writes_only_sanitized_public_artifacts_from_supplied_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            proof = evidence()
            proof.update(secret="private-credential", source_fingerprint=R.source_fingerprint(), checked_at=datetime.now(timezone.utc).isoformat())
            source = path / "input.json"
            source.write_text(json.dumps(proof))
            output = path / "public"
            with patch("sys.stdout", new_callable=StringIO):
                code = CLI.main(["--output-dir", str(output), "--offline-evidence", str(source)])
            self.assertEqual(code, 0)
            self.assertEqual({item.name for item in output.iterdir()}, {"report.json", "index.html"})
            for item in output.iterdir():
                self.assertNotIn("private-credential", item.read_text())

    def test_fingerprint_changes_with_source_but_does_not_read_private_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "module.py").write_text("VALUE = 1\n")
            (root / ".env").write_text("private-token")
            original = R.source_fingerprint(root)
            (root / ".env").write_text("different-private-token")
            self.assertEqual(original, R.source_fingerprint(root))
            (root / "module.py").write_text("VALUE = 2\n")
            self.assertNotEqual(original, R.source_fingerprint(root))


if __name__ == "__main__":
    unittest.main()
