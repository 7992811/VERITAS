"""Daily completion evidence cannot manufacture structural entry time."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
import veritas_timeframe_data as TFD
import veritas_entry_scenarios as S
import veritas_timeframe_structure as TS
from test_veritas_entry_scenarios import rebound_raw, native_days
import veritas_brent_market as B
import veritas_native_daily as ND
import veritas_profinance_history as PF
from test_veritas_brent_market import history as brent_history, quote as brent_quote, IDENTITY as BRENT_IDENTITY

class DailyBoundaryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime.now(timezone.utc).replace(microsecond=0)

    def attach(self, raw):
        with patch("veritas_native_daily.fetch_native_daily",
                   return_value={"bars":raw["native_daily_bars"]}), \
             patch("veritas_profinance_history.fetch_history_bundle") as remote:
            result = TFD.attach(raw, self.clock)
        remote.assert_not_called()
        return result

    def test_confirmed_daily_ma_remains_available_without_native_structural_boundary(self):
        raw = rebound_raw(self.clock)
        daily = raw["native_daily_bars"]
        self.assertTrue(daily)
        self.assertTrue(all(b["interval_boundary_verified"] is False for b in daily))
        raw["native_source_history_attached"] = True
        raw["structure_bars_by_timeframe"]["1d"] = daily
        attached = self.attach(raw)
        self.assertEqual(attached["native_daily_bars"],daily)
        for tf in ("1d","3d","7d"):
            self.assertEqual(attached["structure_bars_by_timeframe"][tf],[])
            self.assertEqual(attached["structure_history_status"][tf]["reason"],
                             "NATIVE_DAILY_INTERVAL_UNVERIFIED")
        features = S.daily_features(attached,self.clock)
        self.assertEqual(features["sma50"],100.)
        self.assertEqual(features["ma_daily_asof_basis"],"PROVIDER_DATE_LABEL_ONLY")
        self.assertFalse(features["ma_daily_interval_boundary_verified"])
        self.assertIsNone(features["ma_daily_verified_close_at"])
        self.assertEqual(features["ma_daily_period_label"],daily[-1]["provider_period_label"])
        self.assertEqual(features["ma_daily_completion_observed_at"],
                         daily[-1]["completion_proof"]["observed_at"])
        context = TFD.context(attached,"5m",self.clock)
        self.assertEqual(context["event"]["event_type"],"DAILY_MA_REBOUND")

    def test_complete_observed_hourly_buckets_have_their_own_verified_utc_boundary(self):
        raw = rebound_raw(self.clock)
        raw["native_source_history_attached"] = True
        raw["structure_bars_by_timeframe"]["1d"] = raw["native_daily_bars"]
        end = int(self.clock.timestamp())//86400*86400
        raw["structure_bars_by_timeframe"]["1h"] = [
            dict(ts=end-48*3600+i*3600,open=200.,high=201.,low=199.,close=200.,
                 timeframe="1h",source_identity=deepcopy(raw["structure_source_identity"]))
            for i in range(48)]
        attached = self.attach(raw)
        days = attached["structure_bars_by_timeframe"]["1d"]
        self.assertEqual(len(days),2)
        self.assertTrue(all(b["interval_boundary_verified"] is True for b in days))
        self.assertTrue(all(b["aggregation"]=="COMPLETE_OBSERVED_BUCKET" for b in days))
        self.assertTrue(all(b["ts"]%86400==0 for b in days))
        self.assertEqual(days[-1]["close"],200.)
        self.assertEqual(S.daily_features(attached,self.clock)["sma50"],100.)

    def test_direct_context_cannot_reissue_date_only_events_at_proof_observation(self):
        for tf in ("1d","3d","7d"):
            raw = rebound_raw(self.clock)
            seconds = TS.timeframe_seconds(tf)
            end = self.clock.timestamp()
            rows = [dict(ts=end-(33-i)*seconds,available_at=end,
                         open=100.,high=100.5,low=99.5,close=100.,
                         timeframe=tf,source_identity=raw["structure_source_identity"],
                         interval_boundary_verified=False,
                         native_time_basis="PROVIDER_DATE_LABEL_ONLY") for i in range(33)]
            rows[24]["high"] = 101.
            rows[27]["low"] = 99.
            rows[-1].update(high=101.3,close=101.2)
            raw["structure_bars_by_timeframe"][tf] = rows
            context = TFD.context(raw,tf,self.clock)
            self.assertIsNone(context.get("event"),context)
            self.assertEqual(context.get("input_issue"),"NATIVE_DAILY_INTERVAL_UNVERIFIED")


    def test_brent_revision_evidence_survives_full_historical_attachment(self):
        history = brent_history()
        daily = native_days(self.clock,BRENT_IDENTITY,
                            known_before=self.clock-timedelta(hours=4))
        observed = self.clock.timestamp()+10
        revised = daily[-20]
        revised["close"] += .2
        revised["completion_proof"].update(observed_at=observed,
                                          ohlc_sha256=PF._daily_ohlc_sha256(revised))
        revised.update(available_at=observed,revision_observed_at=observed)
        history["bars_by_timeframe"]["1d"] = daily
        quote = brent_quote(observed_at=self.clock.isoformat())
        raw = B.build_market(quote,history,self.clock)
        self.assertNotIn(revised["ts"],[r["ts"] for r in raw["structure_bars_by_timeframe"]["1d"]])
        self.assertEqual(raw["native_daily_evidence"][-20]["revision_observed_at"],observed)
        cache = ND.DailyHistoryCache(clock=lambda:observed)
        with patch.object(ND,"_HISTORY",cache), \
             patch("veritas_profinance_history.fetch_history_bundle") as remote:
            attached = TFD.attach(raw,self.clock)
        remote.assert_not_called()
        self.assertEqual(attached["native_daily_history_status"]["status"],
                         "DAILY_REVISION_NOT_YET_KNOWN")
        features = S.daily_features(attached,self.clock)
        self.assertIsNone(features["sma50"])
        self.assertEqual(features["daily_ma_status"],"DAILY_REVISION_NOT_YET_KNOWN")
        known = S.daily_features(attached,observed)
        self.assertEqual(known["daily_ma_status"],"OK")
        self.assertAlmostEqual(known["sma50"],100.+.2/50)
        self.assertEqual(known["ma_daily_known_at"],observed)

    def test_live_history_request_keeps_live_observation_clock(self):
        raw = rebound_raw(self.clock)
        raw.pop("native_source_history_attached",None)
        bundle = dict(source_identity=raw["structure_source_identity"],
                      bars_by_timeframe=raw["structure_bars_by_timeframe"],
                      forming_bars_by_timeframe={},status_by_timeframe={})
        with patch("veritas_profinance_history.fetch_history_bundle",return_value=bundle) as remote, \
             patch("veritas_native_daily.fetch_native_daily",
                   return_value={"bars":raw["native_daily_bars"]}):
            TFD.attach(raw)
        remote.assert_called_once_with(asset="NQ",now=None)

if __name__ == "__main__":
    unittest.main()
