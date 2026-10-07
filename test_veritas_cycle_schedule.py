"""A completed wall-clock cycle is not evidence of current entry candles."""
from datetime import datetime, timezone
import threading
import unittest

import veritas_cycle_schedule as S
import veritas_history_diagnostics as D


ASSETS = ("ETH", "NQ", "MOEX")
NOW = datetime(2026, 10, 7, 7, 59, 50, tzinfo=timezone.utc).timestamp()


def matrix(clock):
    return [{"asset": asset, "horizon": tf, "trade_plan": {
        "timeframe_entry_context": {"status": "OK", "closed_at": int(clock // S.SECONDS[tf]) * S.SECONDS[tf]}}}
        for asset in ASSETS for tf in S.FAST]


class FastReuseTests(unittest.TestCase):
    def test_reuse_ends_at_actual_bar_boundary_even_if_cycle_just_finished(self):
        rows = matrix(NOW)
        self.assertTrue(S.can_reuse_fast(rows, ASSETS, NOW + 5))
        self.assertFalse(S.can_reuse_fast(rows, ASSETS, NOW + 11))
        self.assertTrue(S.can_reuse_fast(matrix(NOW + 11), ASSETS, NOW + 11))

    def test_missing_stale_or_future_asset_cannot_be_certified_by_other_rows(self):
        self.assertFalse(S.can_reuse_fast(matrix(NOW)[:-1], ASSETS, NOW))
        for closed in (None, NOW - 1800, NOW + 90, float("nan")):
            rows = matrix(NOW)
            rows[2]["trade_plan"]["timeframe_entry_context"]["closed_at"] = closed
            self.assertFalse(S.can_reuse_fast(rows, ASSETS, NOW))

    def test_hourly_and_four_hour_refresh_join_next_fast_pass(self):
        attempted = {h: S.boundary(NOW, h) for h in ("1h", "4h")}
        available = ("1m", "5m", "1h", "4h", "1d", "3d", "7d")
        self.assertEqual(S.fast_horizons(attempted, NOW, available), S.FAST)
        self.assertEqual(S.fast_horizons(attempted, NOW + 15, available),
                         ("1m", "5m", "1h", "4h"))
        attempted.update({h: S.boundary(NOW + 15, h) for h in ("1h", "4h")})
        self.assertEqual(S.fast_horizons(attempted, NOW + 40, available), S.FAST)

    def test_invalid_but_recent_context_is_not_reusable(self):
        rows = matrix(NOW)
        rows[0]['trade_plan']['timeframe_entry_context']['status'] = 'INVALID'
        self.assertFalse(S.can_reuse_fast(rows, ASSETS, NOW))

    def test_a_close_during_slow_full_cycle_is_retried_without_waiting_four_minutes(self):
        class Clock:
            current = NOW - 65
            def time(self): return self.current
            def monotonic(self): return self.current
            def sleep(self, seconds): self.current += seconds
        clock, calls = Clock(), []
        def cycle(selected, mode):
            calls.append((selected, mode, clock.current))
            if len(calls) == 3:
                raise KeyboardInterrupt("end deterministic loop")
            clock.current += 75 if mode == "FULL" else 1
        ns = {"time": clock, "cycle": cycle, "HORIZONS": (*S.FAST, "1h", "4h", "1d"),
              "V90_FULL_CYCLE_INTERVAL_SECONDS": 240, "V90_FAST_5M_INTERVAL_SECONDS": 30,
              "lock": threading.Lock(), "last_cycle": {"summary": matrix(NOW)},
              "now": lambda: str(clock.current), "VERSION": "TEST", "emit": lambda *a, **k: None}
        with self.assertRaises(KeyboardInterrupt):
            S.run(ns)
        self.assertEqual(calls[0][:2], (("1m", "5m"), "FAST_5M"))
        self.assertEqual(calls[1][:2], (None, "FULL"))
        self.assertEqual(calls[2][:2], (("1m", "5m", "1h", "4h"), "FAST_5M"))
        self.assertLess(calls[2][2] - calls[1][2], 240)

    def test_overrun_full_cycle_cannot_starve_fast_lane(self):
        class Clock:
            current = NOW
            def time(self): return self.current
            def monotonic(self): return self.current
            def sleep(self, seconds): self.current += seconds
        clock, modes = Clock(), []
        def cycle(selected, mode):
            modes.append(mode)
            if len(modes) == 3:
                raise KeyboardInterrupt('stop deterministic overrun')
            clock.current += 301 if mode == 'FULL' else 1
        ns = {'time':clock, 'cycle':cycle, 'HORIZONS':(*S.FAST, '1h', '4h'),
              'V90_FULL_CYCLE_INTERVAL_SECONDS':300, 'V90_FAST_5M_INTERVAL_SECONDS':30}
        with self.assertRaises(KeyboardInterrupt):
            S.run(ns)
        self.assertEqual(modes, ['FAST_5M', 'FULL', 'FAST_5M'])

    def test_failed_cycle_clears_partial_progress_without_erasing_published_rows(self):
        class Clock:
            current = NOW
            def time(self): return self.current
            def monotonic(self): return self.current
            def sleep(self, seconds): self.current += seconds
        clock, calls = Clock(), []
        existing = matrix(NOW)
        last = {'summary': existing, 'at': 'previous-completed',
                'cycle_in_progress': {'cycle_id': 'partial'}}
        def cycle(selected, mode):
            calls.append(mode)
            if len(calls) == 1:
                raise RuntimeError('failed after partial publication')
            self.assertFalse(last['cycle_in_progress'])
            self.assertIs(last['summary'], existing)
            self.assertEqual(last['at'], 'previous-completed')
            raise KeyboardInterrupt('stop deterministic failure recovery')
        ns = {'time': clock, 'cycle': cycle, 'HORIZONS': S.FAST,
              'V90_FULL_CYCLE_INTERVAL_SECONDS': 300, 'V90_FAST_5M_INTERVAL_SECONDS': 30,
              'lock': threading.Lock(), 'last_cycle': last, 'now': lambda: str(clock.current),
              'VERSION': 'TEST', 'emit': lambda *args, **kwargs: None}
        with self.assertRaises(KeyboardInterrupt):
            S.run(ns)
        self.assertIn('failed after partial publication', last['last_cycle_error']['error'])


class HistoryDiagnosticTests(unittest.TestCase):
    def test_fresh_quote_does_not_renew_history_and_no_session_url_is_exported(self):
        raw = {"observed_at": NOW, "structure_history_status": {"5m": {
            "fetched_at": NOW - 1500, "cache_reused": True,
            "fetch_error": "ReadTimeout: https://example.invalid/history?SID=private-chart-id",
            "reason": "BUDGET_EXHAUSTED"}}}
        result = D.summary(raw, "5m", {"status": "OK", "closed_at": NOW - 1500, "bars": 500}, NOW)
        self.assertEqual(result["status"], "STALE")
        self.assertEqual(result["age_seconds"], 1500)
        self.assertEqual(result["fetched_at"], NOW - 1500)
        self.assertEqual(result["fetch_error"], "ReadTimeout")
        self.assertNotIn("SID", str(result))

    def test_clock_and_candle_absence_are_explicit(self):
        result = D.summary({}, "1h", {}, NOW)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertIsNone(result["last_closed_at"])
        self.assertEqual(result["checked_at"], NOW)
        self.assertEqual(D.summary({}, "5m", {"status":"OK", "closed_at": NOW - 100}, NOW)["status"], "READY")

    def test_fresh_timestamps_do_not_hide_insufficient_or_invalid_context(self):
        result = D.summary({}, '1h', {'status':'UNAVAILABLE',
            'reason':'SAME_TF_HISTORY_INSUFFICIENT', 'closed_at':NOW - 10, 'bars':2}, NOW)
        self.assertTrue(result['timing_fresh'])
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertEqual(result['context_reason'], 'SAME_TF_HISTORY_INSUFFICIENT')


if __name__ == "__main__":
    unittest.main()
