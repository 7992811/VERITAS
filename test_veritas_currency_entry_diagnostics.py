"""Synthetic native contexts; all admissions and Telegram rendering are offline."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest

import veritas_currency_trade_plan as P
from veritas_trade_telegram import readiness_text
from test_veritas_currency_structural_trading import native_fixture, UID
from test_veritas_timeframe_policy import structural_row


def stale_failure():
    now = datetime(2026, 10, 8, 6, 26, 12, tzinfo=timezone.utc)
    _, _, spec, account, quote, _ = native_fixture()
    spec, account, quote = (replace(x, observed_at=now) for x in (spec, account, quote))
    old = structural_row(now-timedelta(minutes=10), asset="CNYRUBF", price=float(quote.ask),
                         source="TBANK_GRPC CNYRUBF", contract={"instrument_uid":UID})
    alternative = structural_row(now, timeframe="1h", asset="CNYRUBF", price=float(quote.ask),
                                 source="TBANK_GRPC CNYRUBF", contract={"instrument_uid":UID})
    alternative.update(research_decision="SHORT", decision="SHORT")
    rows = [old, alternative]
    original = deepcopy(rows)
    try:
        P.select_entry(rows, account, now, spec=spec, quote=quote)
    except P.EntryAdmissionBlocked as exc:
        return exc, rows, original, now
    raise AssertionError("Stale/conflicting synthetic contexts must not be admitted")


class EntryDiagnosticsTests(unittest.TestCase):
    def test_fresh_broker_quote_does_not_hide_stale_native_context_or_other_refusal(self):
        failure, rows, original, now = stale_failure()
        self.assertEqual(str(failure), "SAME_TF_CONTEXT_STALE")
        self.assertEqual(rows, original)
        detail = failure.entry_diagnostics
        self.assertEqual(detail["horizon"], "5m")
        self.assertEqual(detail["context_max_age_seconds"], 300)
        self.assertGreater(detail["context_age_seconds"], 600)
        self.assertEqual(detail["quote_observed_at"], now.isoformat())
        self.assertEqual(P.utc(detail["context_closed_at"]).timestamp(),
                         rows[0]["timeframe_entry_context"]["closed_at"])
        self.assertEqual(detail["routes"], [
            {"horizon": "5m", "selected": True, "reason": "SAME_TF_CONTEXT_STALE"},
            {"horizon": "1h", "selected": False, "reason": "R59_EXECUTION_TF_DIRECTION_CONFLICT"}])
        self.assertNotIn("proof", json.dumps(detail))
        self.assertNotIn("account", json.dumps(detail))

    def test_final_selected_reason_wins_and_quote_rule_does_not_get_legacy_age_limit(self):
        row, _, _, _, _, now = native_fixture()
        row["_currency_route_trace"] = [{"horizon": "1h", "direction": "LONG",
            "reason": "ADMITTED", "open": True, "secret": "must-not-be-copied"}] * 100
        original = deepcopy(row)
        failure = P.EntryAdmissionBlocked(row, {"reason": "CURRENCY_DRAWDOWN_STOP"}, now)
        detail = failure.entry_diagnostics
        self.assertIsNone(detail["context_max_age_seconds"])
        self.assertEqual(detail["routes"][0]["reason"], "CURRENCY_DRAWDOWN_STOP")
        self.assertEqual(len(detail["routes"]), 7)
        self.assertNotIn("must-not-be-copied", json.dumps(detail))
        self.assertEqual(row, original)

    def test_invalid_decision_clock_retains_the_actual_refusal(self):
        row, _, _, account, _, _ = native_fixture()
        with self.assertRaisesRegex(P.EntryAdmissionBlocked, "SAME_TF_DECISION_TIME_REQUIRED") as raised:
            P.select_entry([row], account, "invalid-clock")
        self.assertIsNone(raised.exception.entry_diagnostics["checked_at"])
        self.assertIsNone(raised.exception.entry_diagnostics["context_age_seconds"])

    def test_telegram_distinguishes_old_candle_from_current_book_and_model_not_checked(self):
        failure, _, _, now = stale_failure()
        status = {"ok": True, "enabled": True, "execution_enabled": True,
            "execution_environment": "production", "account_id": "synthetic-7745",
            "binding_state": "bound", "last_poll_at": now.isoformat(),
            "last_poll_block_reason": str(failure), "last_poll_succeeded": True,
            "new_risk_block_reason": "LIVE_MODEL_EVIDENCE_NOT_CHECKED",
            "last_poll_entry_diagnostics": failure.entry_diagnostics}
        original = deepcopy(status)
        text = readiness_text(status)
        self.assertIn("Свечи выбранного таймфрейма устарели", text)
        self.assertIn("CNYRUBf · 5m · покупка", text)
        self.assertIn("Возраст свечи при проверке: 610 с; допустимо: 300 с", text)
        self.assertIn("Котировка при проверке: 08.10.2026 09:26:12 МСК", text)
        self.assertIn("Другой вариант, 1h: Направление сигнала противоречит структуре", text)
        self.assertIn("Допуск модели к реальным сделкам ещё не проверен", text)
        self.assertEqual(status, original)
        self.assertNotIn("synthetic-7745", text)
        self.assertLess(len(text), 4096)
        status["last_poll_entry_diagnostics"] = {"horizon": [], "direction": {}}
        self.assertNotIn("Проверенный сигнал:", readiness_text(status))


if __name__ == "__main__":
    unittest.main()
