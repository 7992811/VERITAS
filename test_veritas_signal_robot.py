from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import patch

import httpx

import veritas_signal_robot as R


KEY = "k" * 40
BOT = 123456789
BASE = "https://veritas-intelligence-v1.onrender.com"


def row(event="event-1", direction="LONG", horizon="5m", stale=False):
    return {
        "asset": "CNYRUBF", "horizon": horizon, "research_decision": direction,
        "source_gate_pass": True, "snapshot_stale": stale,
        "timeframe_entry_context": {"event": {
            "event_id": event, "direction": direction,
            "signal_at": datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp(),
        }},
    }


class SignalRobotTests(unittest.TestCase):
    def transport(self, signals, trade=None, statuses=None):
        seen = []
        statuses = statuses or {}
        trade = trade or {
            "ok": True, "enabled": True, "execution_enabled": True,
            "sandbox_autotrade_enabled": True, "items": [], "block_reason": None,
        }
        def handler(request):
            seen.append((request.method, request.url.path,
                         json.loads(request.content) if request.content else None,
                         dict(request.headers)))
            if request.url.path == R.SIGNALS_PATH:
                return httpx.Response(statuses.get("signals", 200),
                                      json={"signals": signals})
            if request.url.path == R.TRADE_POLL_PATH:
                return httpx.Response(statuses.get("trade", 200), json=trade)
            return httpx.Response(404, json={})
        client = httpx.Client(transport=httpx.MockTransport(handler),
                              follow_redirects=False, trust_env=False)
        return client, seen

    def test_selects_newest_verified_currency_event_only(self):
        older = row("old", "LONG", "1h")
        older["timeframe_entry_context"]["event"]["signal_at"] -= 60
        newest = row("new", "SHORT", "5m")
        foreign = dict(row("foreign"), asset="BTC")
        stale = row("stale", stale=True)
        selected = R.select_currency_signal({"signals": [older, foreign, stale, newest]})
        self.assertEqual(selected["timeframe_entry_context"]["event"]["event_id"], "new")
        self.assertEqual(selected["research_decision"], "SHORT")

    def test_tick_reads_veritas_signal_then_ticks_internal_trade_service(self):
        client, seen = self.transport([row()])
        logs = []
        robot = R.VeritasSignalRobot(BASE, KEY, BOT, client=client, log=logs.append)
        result = robot.tick()
        self.assertEqual(result["signal"]["asset"], "CNYRUBF")
        self.assertTrue(result["signal_changed"])
        self.assertEqual([(m, p) for m, p, _, _ in seen],
                         [("GET", R.SIGNALS_PATH), ("POST", R.TRADE_POLL_PATH)])
        self.assertEqual(seen[1][2], {"bot_id": BOT})
        self.assertEqual(seen[1][3]["x-veritas-trade-key"], KEY)
        self.assertIn("veritas_signal_robot_tick", logs[-1])
        robot.close()

    def test_same_signal_remains_idempotent_but_service_still_ticks_for_exits(self):
        client, seen = self.transport([row()])
        robot = R.VeritasSignalRobot(BASE, KEY, BOT, client=client, log=lambda *_: None)
        self.assertTrue(robot.tick()["signal_changed"])
        self.assertFalse(robot.tick()["signal_changed"])
        self.assertEqual(sum(path == R.TRADE_POLL_PATH for _, path, _, _ in seen), 2)
        robot.close()

    def test_no_directional_signal_still_ticks_service_for_position_management(self):
        client, seen = self.transport([dict(row(), research_decision="NO_TRADE")])
        robot = R.VeritasSignalRobot(BASE, KEY, BOT, client=client, log=lambda *_: None)
        self.assertIsNone(robot.tick()["signal"])
        self.assertEqual(seen[-1][1], R.TRADE_POLL_PATH)
        robot.close()

    def test_production_fails_closed_if_server_exposes_sandbox_auto_mode(self):
        client, unused = self.transport([row()], trade={
            "ok": True, "enabled": True, "execution_enabled": True,
            "sandbox_autotrade_enabled": True, "items": [],
        })
        robot = R.VeritasSignalRobot(BASE, KEY, BOT, environment="production",
                                     client=client, log=lambda *_: None)
        with self.assertRaisesRegex(R.SignalRobotError, "SANDBOX_AUTOTRADE_PRODUCTION_FORBIDDEN"):
            robot.tick()
        robot.close()

    def test_invalid_origins_keys_and_environment_are_rejected(self):
        for url in ("http://veritas-intelligence-v1.onrender.com",
                    "https://example.com", BASE + "/api", "https://u:p@" + R.EXPECTED_HOST):
            with self.subTest(url=url):
                with self.assertRaises(R.SignalRobotError):
                    R.VeritasSignalRobot(url, KEY, BOT)
        with self.assertRaises(R.SignalRobotError):
            R.VeritasSignalRobot(BASE, "short", BOT)
        with self.assertRaises(R.SignalRobotError):
            R.VeritasSignalRobot(BASE, KEY, BOT, environment="live")

    def test_build_from_env_is_default_off_and_uses_bounded_interval(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(R.build_from_env())
            self.assertEqual(R._seconds("0"), 1.0)
            self.assertEqual(R._seconds("99"), 5.0)
            self.assertEqual(R._seconds("bad"), 2.0)


if __name__ == "__main__":
    unittest.main()
