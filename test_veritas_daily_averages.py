"""Native daily arithmetic and source/time provenance regressions."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import unittest

import veritas_daily_averages as DA

DAY = 86400
PF = {"key": "PROFINANCE:NASD100_FUT", "contract_id": None, "asset": "NQ"}
BTC = {"key": "BINANCE:BTCUSDT", "contract_id": None, "asset": "BTC"}


def fixture(count=240, identity=None):
    identity = identity or PF
    return [{"ts": i * DAY, "end_ts": (i+1) * DAY, "available_at": (i+1) * DAY,
             "timeframe": "1d", "native_timeframe": "1d", "aggregation": "NATIVE",
             "source_identity": deepcopy(identity), "open": 100+i*.1,
             "high": 101+i*.1, "low": 99+i*.1, "close": 100+i*.1}
            for i in range(count)]


class DailyAveragesTests(unittest.TestCase):
    def ctx(self, bars=None, now=None, identity=PF, **kwargs):
        bars = fixture() if bars is None else bars
        now = 240*DAY+3600 if now is None else now
        return DA.build_context(bars, now, asset=identity.get("asset", "NQ"),
                                source_identity=identity, **kwargs)

    def test_exact_independent_periods_and_true_ranges(self):
        c = self.ctx()
        self.assertEqual(c["status"], "OK")
        for n in (18, 50, 200):
            p = c["periods"][str(n)]
            expected = math.fsum(100+i*.1 for i in range(240-n, 240))/n
            self.assertAlmostEqual(p["value"], expected)
            self.assertAlmostEqual(p["slope_points"], .1)
            self.assertAlmostEqual(p["slope_points_5d"], .5)
            self.assertAlmostEqual(p["slope_atr_5d"], .25)
            self.assertEqual(p["crossings_10d"], 0)
        self.assertAlmostEqual(c["atr20"], 2.)
        self.assertEqual(c["daily_asof"], 240*DAY)
        self.assertEqual(c["last_daily_open_at"], 239*DAY)
        self.assertEqual(c["known_at"], 240*DAY)

    def test_50_is_available_without_200(self):
        c = self.ctx(fixture(90), 90*DAY+3600)
        self.assertEqual(c["status"], "OK")
        self.assertEqual(c["periods"]["50"]["status"], "OK")
        self.assertIsNone(c["periods"]["200"]["value"])
        self.assertEqual(c["periods"]["200"]["required_count"], 200)
        self.assertEqual(c["periods"]["200"]["status"], "INSUFFICIENT_DAILY_HISTORY")

    def test_exactly_200_does_not_invent_slope_or_crossings(self):
        c = self.ctx(fixture(200), 200*DAY+1)
        self.assertEqual(c["periods"]["200"]["status"], "OK")
        for field in ("previous_value", "slope", "slope_points_5d", "slope_atr_5d", "crossings_10d"):
            self.assertIsNone(c["periods"]["200"][field])

    def test_future_daily_close_cannot_change_touch_snapshot(self):
        bars = fixture(241)
        asof = 235*DAY+3600
        expected = self.ctx(bars[:235], asof)
        for b in bars[235:]:
            b.update(open=9999., high=10000., low=9998., close=9999.)
        actual = self.ctx(bars, asof)
        self.assertEqual(actual["periods"], expected["periods"])
        self.assertEqual(actual["digest"], expected["digest"])
        self.assertLessEqual(actual["known_at"], asof)

    def test_available_at_controls_knowledge_even_after_close(self):
        bars = fixture()
        bars[-1]["available_at"] = 240*DAY+7200
        c = self.ctx(bars, 240*DAY+3600)
        self.assertEqual(c["daily_asof"], 239*DAY)
        self.assertEqual(c["known_at"], 239*DAY)
        self.assertAlmostEqual(c["sma50"], math.fsum(100+i*.1 for i in range(189,239))/50)

    def test_source_mismatch_taints_window_without_using_foreign_prices(self):
        bars = fixture()
        bars[-10]["source_identity"] = {"key": "YAHOO:NQ=F"}
        c = self.ctx(bars)
        self.assertIsNone(c["periods"]["50"]["value"])
        self.assertEqual(c["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_bad_old_source_only_blocks_window_containing_it(self):
        bars = fixture()
        bars[100]["source_identity"] = {"key": "YAHOO:NQ=F"}
        c = self.ctx(bars)
        self.assertEqual(c["periods"]["50"]["status"], "OK")
        self.assertEqual(c["periods"]["200"]["status"], "INVALID_DAILY_WINDOW")

    def test_contract_mismatch_is_rejected(self):
        identity = {"key":"TBANK_GRPC:CNYRUBF", "contract_id":"expected-uid", "asset":"CNYRUBF"}
        bars = fixture(identity=identity)
        bars[-1]["source_identity"]["contract_id"] = "another-uid"
        self.assertIsNone(self.ctx(bars, identity=identity)["periods"]["50"]["value"])

    def test_hourly_group_aggregation_cannot_be_daily_average(self):
        bars = fixture()
        for b in bars:
            b.update(aggregation="COMPLETE_OBSERVED_BUCKET", source_bar_count=24)
        c = self.ctx(bars)
        self.assertIsNone(c["sma50"])
        self.assertIsNone(c["sma200"])

    def test_wrong_timeframe_and_missing_native_proof_rejected(self):
        for mutation in ({"timeframe":"1h"}, {"native_timeframe":None}):
            with self.subTest(mutation=mutation):
                bars = fixture()
                for b in bars:
                    b.update(mutation)
                self.assertIsNone(self.ctx(bars)["sma50"])

    def test_native_flag_cannot_promote_an_hour_to_a_daily_candle(self):
        bars = fixture()
        for b in bars:
            b["end_ts"] = b["available_at"] = b["ts"]+3600
        self.assertIsNone(self.ctx(bars)["sma50"])

    def test_conflicting_duplicate_does_not_silently_skip_one_day(self):
        bars = fixture()
        copy = deepcopy(bars[-20])
        copy["close"] += .2
        c = self.ctx(bars+[copy])
        self.assertEqual(c["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")
        self.assertEqual(c["diagnostics"]["conflicting_timestamps"], 1)

    def test_identical_duplicates_and_input_order_do_not_change_digest(self):
        bars = fixture()
        expected = self.ctx(bars)
        actual = self.ctx(list(reversed(bars))+[deepcopy(bars[-2])])
        self.assertEqual(expected["digest"], actual["digest"])
        self.assertEqual(expected["periods"], actual["periods"])

    def test_synthetic_and_incomplete_closed_rows_do_not_certify_average(self):
        for mutation in ({"synthetic":True}, {"finalized":False}):
            bars = fixture()
            bars[-1].update(mutation)
            self.assertIsNone(self.ctx(bars)["sma50"])

    def test_invalid_ohlc_taints_recent_window(self):
        bars = fixture()
        bars[-5]["low"] = bars[-5]["high"]+1
        self.assertEqual(self.ctx(bars)["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_binance_missing_day_invalidates_window_without_fill(self):
        bars = fixture(identity=BTC)
        del bars[-10]
        self.assertEqual(self.ctx(bars, identity=BTC)["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_native_nontrading_days_need_not_be_fabricated(self):
        bars = fixture()
        for i,b in enumerate(bars):
            b["ts"] = (i + 2*(i//5))*DAY
            b["end_ts"] = b["available_at"] = b["ts"]+DAY
        c = self.ctx(bars, bars[-1]["end_ts"]+3600)
        self.assertEqual(c["periods"]["200"]["status"], "OK")
        self.assertEqual(c["provenance"]["bar_count"], 240)

    def test_long_native_exchange_holiday_is_not_synthetic_missing_data(self):
        bars = fixture()
        for b in bars[120:]:
            for key in ("ts","end_ts","available_at"):
                b[key] += 10*DAY
        c = self.ctx(bars, bars[-1]["end_ts"]+1)
        self.assertEqual(c["periods"]["200"]["status"], "OK")

    def test_declared_asset_cannot_disagree_with_source_identity(self):
        c = DA.build_context(fixture(), 240*DAY+1, asset="GOLD", source_identity=PF)
        self.assertEqual(c["status"], "DAILY_ASSET_IDENTITY_MISMATCH")
        self.assertIsNone(c["sma50"])

    def test_stale_native_history_is_visible_but_unavailable(self):
        c = self.ctx(now=245*DAY)
        self.assertEqual(c["status"], "STALE_DAILY_HISTORY")
        self.assertIsNone(c["sma50"])
        self.assertIsNone(c["atr20"])
        self.assertEqual(c["daily_asof"], 240*DAY)

    def test_valid_until_certifies_cache_freshness_boundary(self):
        c = self.ctx()
        self.assertEqual(c["valid_until"], 240*DAY+4*DAY+60)
        self.assertEqual(self.ctx(now=c["valid_until"])["status"], "OK")
        self.assertEqual(self.ctx(now=c["valid_until"]+1)["status"], "STALE_DAILY_HISTORY")
        crypto = self.ctx(fixture(identity=BTC), identity=BTC)
        self.assertEqual(crypto["valid_until"], 240*DAY+DAY+60)
        customized = self.ctx(config={"max_daily_age_days":2})
        self.assertEqual(customized["valid_until"], 240*DAY+2*DAY+60)
        self.assertIsNone(self.ctx([])["valid_until"])

    def test_invalid_decision_time_fails_closed(self):
        for at in (True, float("nan"), "2026-10-07T10:00:00", datetime(2026,10,7)):
            with self.subTest(at=str(at)):
                c = DA.build_context(fixture(), at, asset="NQ", source_identity=PF)
                self.assertEqual(c["status"], "INVALID_DECISION_TIME")
                self.assertIsNone(c["sma200"])

    def test_aware_datetime_and_numeric_time_agree(self):
        epoch = 240*DAY+3600
        self.assertEqual(self.ctx(now=epoch)["digest"],
                         self.ctx(now=datetime.fromtimestamp(epoch, timezone.utc))["digest"])

    def test_digest_does_not_depend_on_unused_older_history(self):
        bars = fixture(300)
        a = self.ctx(bars, 300*DAY+1)
        b = self.ctx(bars[-210:], 300*DAY+1)
        self.assertEqual(a["digest"], b["digest"])

    def test_five_day_slope_and_daily_crossings_are_causal(self):
        bars = fixture()
        for i,b in enumerate(bars):
            close = 99 if i%2 == 0 else 101
            b.update(open=close, high=close+1, low=close-1, close=close)
        c = self.ctx(bars)
        self.assertAlmostEqual(c["periods"]["50"]["slope_points_5d"], 0)
        self.assertEqual(c["periods"]["50"]["crossings_10d"], 9)

    def test_explicit_invalid_available_time_is_not_ignored(self):
        bars = fixture()
        bars[-1]["available_at"] = "invalid"
        self.assertEqual(self.ctx(bars)["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_caller_data_is_never_mutated(self):
        bars = fixture()
        before = deepcopy(bars)
        self.ctx(bars)
        self.assertEqual(bars, before)


if __name__ == "__main__":
    unittest.main()
