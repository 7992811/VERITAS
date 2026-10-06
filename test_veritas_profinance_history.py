"""Safety boundaries for native ProFinance structural history (no live IO)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import time
import unittest

import httpx

import veritas_price_source as VPS
import veritas_profinance_history as H


HEADER = ";Open;High;Low;Close;Время\n"
NOW = datetime(2026, 10, 6, 19, 28, 2, tzinfo=timezone.utc).timestamp()
REFRESH = (
    "1;publicSession\ns;name\nt;{ticker}\nn;Last;2\nn;Bid;0\nn;Ask;1\n"
    "6;1Min;1;bar\n6;5Min;3;bar\n6;1Hour;6;bar\n6;4Hour;8;bar\n6;1Day;9;bar\n"
)


class Clock:
    def __init__(self, now=NOW):
        self.now, self.elapsed = now, 0.

    def advance(self, seconds):
        self.now += seconds
        self.elapsed += seconds


def history_text(tt, now, count=80):
    seconds = {1: 60, 3: 300, 6: 3600, 8: 14400, 9: 86400}[tt]
    local = datetime.fromtimestamp(now, H.MOSCOW)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() if tt == 9 else int(now)//seconds*seconds
    fmt = "%d.%m.%Y" if tt == 9 else "%d.%m.%Y %H:%M" if tt in (1, 3) else "%d.%m.%Y %H"
    return HEADER + "\n".join(
        ";100.0;102.0;99.0;101.0;" + datetime.fromtimestamp(end-(i+1)*seconds, H.MOSCOW).strftime(fmt)
        for i in range(count))


class NativeParserTests(unittest.TestCase):
    def test_observed_nq_five_minute_data_retains_price_and_source_identity(self):
        # Verbatim OHLC rows retrieved from the public NQ chart on 2026-10-06.
        text = HEADER + (
            ";31610.50;31610.88;31594.75;31599.00;06.10.2026 18:20\n"
            "#52F6A4;31612.00;31615.38b;31599.25;31610.25;06.10.2026 18:15\n"
        )
        cutoff = datetime(2026, 10, 6, 15, 20, tzinfo=timezone.utc)
        parsed = H.parse_history(text, "NQ", "5m", cutoff)
        self.assertEqual(len(parsed["bars"]), 1)
        bar = parsed["bars"][0]
        self.assertEqual(bar["high"], 31615.38)
        self.assertEqual(bar["available_at"], cutoff.timestamp())
        self.assertEqual(bar["ts"], cutoff.timestamp()-300)
        self.assertIsNone(bar["volume"])
        self.assertFalse(bar["volume_available"])
        self.assertTrue(VPS.same(VPS.identity("NQ", {"source": "ProFinance", "raw_label": "NASD100_FUT"}), bar["source_identity"]))
        self.assertEqual(parsed["excluded_partial_rows"], 1)

    def test_four_hour_candle_is_closed_only_at_native_utc_boundary(self):
        text = HEADER + ";100;102;99;101;06.10.2026 19\n;100;102;99;101;06.10.2026 15\n"
        bars = H.parse_history(text, "NQ", "4h", NOW)["bars"]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["ts"], datetime(2026, 10, 6, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(bars[0]["end_ts"], datetime(2026, 10, 6, 16, tzinfo=timezone.utc).timestamp())

    def test_partial_evidence_requires_explicit_actual_observation_time(self):
        text = HEADER + ";100;102;99;101;06.10.2026 19\n;100;102;99;101;06.10.2026 15\n"
        historical = H.parse_history(text, "NQ", "4h", NOW)
        self.assertEqual(historical["forming_bars"], [])
        observed = H.parse_history(text, "NQ", "4h", NOW, observed_at=NOW)
        self.assertEqual(len(observed["forming_bars"]), 1)
        self.assertFalse(observed["forming_bars"][0]["finalized"])
        self.assertEqual(observed["forming_bars"][0]["observed_at"], NOW)
        future = H.parse_history(text, "NQ", "4h", NOW, observed_at=NOW+60)
        self.assertEqual(future["forming_bars"], [])

    def test_daily_candle_keeps_provider_calendar_date_and_excludes_current_day(self):
        text = HEADER + ";100;102;99;101;06.10.2026\n;100;102;99;101;05.10.2026\n"
        bars = H.parse_history(text, "NQ", "1d", NOW)["bars"]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["end_ts"], datetime(2026, 10, 5, 21, tzinfo=timezone.utc).timestamp())

    def test_conflicting_duplicates_and_malformed_ohlc_never_become_levels(self):
        text = HEADER + (
            ";100;102;99;101;06.10.2026 22:20\n"
            ";100;103;99;101;06.10.2026 22:20\n"
            ";nan;102;99;101;06.10.2026 22:21\n"
            ";100;98;99;101;06.10.2026 22:22\n"
            ";100;102;99;101;06.10.2026 22:23\n"
        )
        result = H.parse_history(text, "NQ", "1m", NOW)
        self.assertEqual(result["conflicting_timestamps"], 1)
        self.assertEqual(result["rejected_rows"], 2)
        self.assertEqual(len(result["bars"]), 1)
        self.assertEqual(datetime.fromtimestamp(result["bars"][0]["ts"], H.MOSCOW).minute, 23)

    def test_response_and_retained_history_have_hard_limits(self):
        self.assertEqual(len(H.parse_history(history_text(1, NOW, 1440), "NQ", "1m", NOW)["bars"]), 500)
        with self.assertRaisesRegex(ValueError, "TOO_LARGE"):
            H.parse_history(HEADER + "x" * H.MAX_RESPONSE_BYTES, "NQ", "1m", NOW)
        with self.assertRaisesRegex(ValueError, "INVALID_HEADER"):
            H.parse_history("Access denied", "NQ", "1m", NOW)

    def test_exact_chart_aliases_and_last_price_are_verified_before_history(self):
        for asset, ticker in (("NQ", "NASD100_FUT"), ("GOLD", "gold"), ("BRENT", "brent")):
            session = H.parse_refresh(REFRESH.format(ticker=ticker), asset)
            self.assertEqual(session["ba"], 2)
            with self.assertRaisesRegex(ValueError, "INSTRUMENT_MISMATCH"):
                H.parse_refresh(REFRESH.format(ticker="NASD100"), asset)
            with self.assertRaisesRegex(ValueError, "PROTOCOL_MISMATCH"):
                H.parse_refresh(REFRESH.format(ticker=ticker).replace("n;Last;2", "n;Last;0"), asset)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.failure = None
        self.cost = 0.
        self.cache = H.HistoryCache(self.fetch, lambda: self.clock.now, lambda: self.clock.elapsed)

    def fetch(self, url, params, remaining):
        self.calls.append((url, dict(params)))
        self.clock.advance(self.cost)
        if url.endswith("refresh"):
            ticker = {"NASD100_FUT": "NASD100_FUT", "Gold": "gold", "Brent oil": "brent"}[params["s"]]
            return REFRESH.format(ticker=ticker)
        if self.failure:
            raise self.failure
        self.assertEqual(params["ba"], 2)
        return history_text(params["tt"], self.clock.now)

    def test_cache_is_reused_but_new_completed_period_forces_refresh(self):
        self.clock.now = datetime(2026, 10, 6, 19, 29, 40, tzinfo=timezone.utc).timestamp()
        first = self.cache.fetch_bundle("NQ", ("5m",))
        self.assertEqual(first["status_by_timeframe"]["5m"]["status"], "READY")
        count = len(self.calls)
        self.clock.advance(10)
        second = self.cache.fetch_bundle("NQ", ("5m",))
        self.assertEqual(len(self.calls), count)
        self.assertTrue(second["status_by_timeframe"]["5m"]["cache_reused"])
        self.clock.advance(20)
        third = self.cache.fetch_bundle("NQ", ("5m",))
        self.assertEqual(len(self.calls), count+1)
        self.assertGreater(third["bars_by_timeframe"]["5m"][-1]["ts"], first["bars_by_timeframe"]["5m"][-1]["ts"])

    def test_failed_refresh_preserves_original_age_and_never_substitutes_source(self):
        first = self.cache.fetch_bundle("NQ", ("1m",))
        at = first["status_by_timeframe"]["1m"]["fetched_at"]
        self.clock.advance(300)
        self.failure = TimeoutError("provider timeout")
        out = self.cache.fetch_bundle("NQ", ("1m", "5m"))
        self.assertEqual(out["status_by_timeframe"]["1m"]["status"], "STALE")
        self.assertEqual(out["status_by_timeframe"]["1m"]["fetched_at"], at)
        self.assertEqual(out["status_by_timeframe"]["5m"]["status"], "UNAVAILABLE")
        self.assertEqual(out["source_key"], "PROFINANCE:NASD100_FUT")
        self.assertIn("TimeoutError", out["status_by_timeframe"]["1m"]["fetch_error"])

    def test_caller_cannot_mutate_cached_prices_or_nested_identity(self):
        first = self.cache.fetch_bundle("GOLD", ("1h",))
        first["bars_by_timeframe"]["1h"][-1]["high"] = 99999
        first["bars_by_timeframe"]["1h"][-1]["source_identity"]["key"] = "YAHOO:GC=F"
        second = self.cache.fetch_bundle("GOLD", ("1h",))
        bar = second["bars_by_timeframe"]["1h"][-1]
        self.assertEqual(bar["high"], 102.)
        self.assertEqual(bar["source_identity"]["key"], "PROFINANCE:Gold")

    def test_budget_loads_fast_native_periods_first_and_resumes_senior_next_cycle(self):
        self.cost = 1.2
        first = self.cache.fetch_bundle("NQ", budget_seconds=3.5)
        requested = [p["tt"] for u, p in self.calls if u.endswith("history")]
        self.assertEqual(requested, [1, 3])
        self.assertEqual(first["status_by_timeframe"]["1h"]["status"], "UNAVAILABLE")
        self.assertEqual(first["status_by_timeframe"]["1h"]["reason"], "BUDGET_EXHAUSTED")
        self.clock.advance(6)
        self.calls.clear()
        second = self.cache.fetch_bundle("NQ", budget_seconds=4)
        requested = [p["tt"] for u, p in self.calls if u.endswith("history")]
        self.assertEqual(requested, [6, 8, 9])
        self.assertTrue(all(x["status"] == "READY" for x in second["status_by_timeframe"].values()))


    def _slow_daily_fetch(self, url, params, remaining):
        self.calls.append((url, dict(params)))
        if url.endswith("refresh"):
            self.clock.advance(.1)
            return REFRESH.format(ticker="brent")
        if params["tt"] == 9:
            self.clock.advance(remaining)
            raise TimeoutError("daily history exceeded the shared budget")
        self.clock.advance(.2)
        return history_text(params["tt"], self.clock.now)

    def test_slow_daily_fetch_cannot_starve_minute_and_five_minute_data(self):
        cache = H.HistoryCache(self._slow_daily_fetch,
                              lambda: self.clock.now, lambda: self.clock.elapsed)
        result = cache.fetch_bundle("BRENT", budget_seconds=2.)
        requested = [p["tt"] for u, p in self.calls if u.endswith("history")]
        self.assertEqual(requested, [1, 3, 6, 8, 9])
        for tf in ("1m", "5m"):
            self.assertTrue(result["bars_by_timeframe"][tf])
            self.assertEqual(result["status_by_timeframe"][tf]["status"], "READY")
        self.assertEqual(result["status_by_timeframe"]["1d"]["status"], "UNAVAILABLE")
        self.assertIn("TimeoutError", result["status_by_timeframe"]["1d"]["fetch_error"])
        self.assertAlmostEqual(self.clock.elapsed, 2.)

    def test_daily_timeout_retry_cannot_preempt_expired_minute_cache(self):
        cache = H.HistoryCache(self._slow_daily_fetch,
                              lambda: self.clock.now, lambda: self.clock.elapsed)
        first = cache.fetch_bundle("BRENT", budget_seconds=2.)
        first_minute_fetch = first["status_by_timeframe"]["1m"]["fetched_at"]
        self.clock.advance(16)
        self.calls.clear()
        second = cache.fetch_bundle("BRENT", budget_seconds=2.)
        requested = [p["tt"] for u, p in self.calls if u.endswith("history")]
        self.assertEqual(requested, [1, 9])
        self.assertGreater(second["status_by_timeframe"]["1m"]["fetched_at"], first_minute_fetch)
        self.assertEqual(second["status_by_timeframe"]["1m"]["status"], "READY")
        self.assertTrue(second["status_by_timeframe"]["5m"]["cache_reused"])
        self.assertEqual(second["status_by_timeframe"]["1d"]["status"], "UNAVAILABLE")
        self.assertFalse(second["bars_by_timeframe"]["1d"])

    def test_access_denial_stops_other_timeframes_and_backs_off(self):
        response = httpx.Response(403, request=httpx.Request("GET", H.BASE+"history"))
        self.failure = httpx.HTTPStatusError("Forbidden", request=response.request, response=response)
        out = self.cache.fetch_bundle("NQ")
        self.assertEqual(len([p for u, p in self.calls if u.endswith("history")]), 1)
        self.assertTrue(all(x["status"] == "UNAVAILABLE" for x in out["status_by_timeframe"].values()))
        self.clock.advance(30)
        self.calls.clear()
        self.cache.fetch_bundle("NQ")
        self.assertEqual(self.calls, [])

    def test_asof_filter_does_not_expose_later_cache_bars(self):
        self.cache.fetch_bundle("NQ", ("1m",))
        earlier = self.clock.now - 600
        out = self.cache.fetch_bundle("NQ", ("1m",), now=earlier)
        self.assertTrue(all(bar["available_at"] <= earlier for bar in out["bars_by_timeframe"]["1m"]))


class ConcurrencyTests(unittest.TestCase):
    def test_same_asset_fetch_is_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def fetch(url, params, remaining):
            calls.append(url)
            if url.endswith("refresh"):
                entered.set()
                self.assertTrue(release.wait(2))
                return REFRESH.format(ticker="NASD100_FUT")
            return history_text(params["tt"], NOW)
        cache = H.HistoryCache(fetch, lambda: NOW)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(cache.fetch_bundle, "NQ", ("1m",))
            self.assertTrue(entered.wait(1))
            second = cache.fetch_bundle("NQ", ("1m",))
            self.assertEqual(second["status_by_timeframe"]["1m"]["reason"], "FETCH_IN_PROGRESS")
            release.set()
            self.assertEqual(first.result(timeout=2)["status_by_timeframe"]["1m"]["status"], "READY")
        self.assertEqual(len(calls), 2)

    def test_process_http_concurrency_does_not_exceed_two(self):
        lock = threading.Lock()
        counters = {"active": 0, "max": 0}
        def fetch(url, params, remaining):
            with lock:
                counters["active"] += 1
                counters["max"] = max(counters["max"], counters["active"])
            try:
                time.sleep(.02)
                if url.endswith("refresh"):
                    ticker = {"NASD100_FUT": "NASD100_FUT", "Gold": "gold", "Brent oil": "brent"}[params["s"]]
                    return REFRESH.format(ticker=ticker)
                return history_text(params["tt"], NOW)
            finally:
                with lock:
                    counters["active"] -= 1
        cache = H.HistoryCache(fetch, lambda: NOW)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda asset: cache.fetch_bundle(asset, ("1m",)), ("NQ", "GOLD", "BRENT")))
        self.assertEqual(counters["max"], 2)
        self.assertTrue(all(r["status_by_timeframe"]["1m"]["status"] == "READY" for r in results))


if __name__ == "__main__":
    unittest.main()
