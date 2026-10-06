"""Native daily arithmetic and source/time provenance regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import unittest

import veritas_daily_averages as DA

DAY = 86400
PF = {"key": "PROFINANCE:NASD100_FUT", "contract_id": None, "asset": "NQ"}
BTC = {"key": "BINANCE:BTCUSDT", "contract_id": None, "asset": "BTC"}


def certify_profinance(row, observed_at=None):
    """Synthetic provider observation; never infer real PF availability here."""
    observed_at = row["end_ts"] if observed_at is None else observed_at
    label = datetime.fromtimestamp(row["ts"], timezone.utc).date()
    identity = row["source_identity"]
    row.update(
        native_timeframe="1d", native_interval=9,
        native_time_basis="PROVIDER_DATE_LABEL_ONLY", interval_boundary_verified=False,
        provider_period_label=label.isoformat(),
        available_at=max(row["end_ts"], observed_at),
        completion_proof={
            "kind": "NEXT_NATIVE_DAILY_OBSERVED",
            "period_label": label.isoformat(),
            "successor_label": (label+timedelta(days=1)).isoformat(),
            "observed_at": observed_at,
            "source_identity": {"key": identity["key"], "contract_id": identity.get("contract_id")},
            "ohlc_sha256": hashlib.sha256(json.dumps(
                [float(row[k]) for k in ("open", "high", "low", "close")],
                separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        })
    return row


def fixture(count=240, identity=None):
    identity = identity or PF
    bars = [{"ts": i * DAY, "end_ts": (i+1) * DAY, "available_at": (i+1) * DAY,
             "timeframe": "1d", "native_timeframe": "1d", "aggregation": "NATIVE",
             "source_identity": deepcopy(identity), "open": 100+i*.1,
             "high": 101+i*.1, "low": 99+i*.1, "close": 100+i*.1}
            for i in range(count)]
    if str(identity.get("key", "")).startswith("PROFINANCE:"):
        for row in bars:
            certify_profinance(row)
    return bars


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
        certify_profinance(copy)
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
            certify_profinance(b)
        c = self.ctx(bars, bars[-1]["end_ts"]+3600)
        self.assertEqual(c["periods"]["200"]["status"], "OK")
        self.assertEqual(c["provenance"]["bar_count"], 240)

    def test_long_native_exchange_holiday_is_not_synthetic_missing_data(self):
        bars = fixture()
        for b in bars[120:]:
            for key in ("ts","end_ts","available_at"):
                b[key] += 10*DAY
            certify_profinance(b)
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
            certify_profinance(b)
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


    def test_profinance_legacy_native_flag_without_completion_is_not_enough(self):
        for field in ("completion_proof", "provider_period_label", "native_time_basis",
                      "interval_boundary_verified", "native_interval"):
            with self.subTest(field=field):
                bars = fixture()
                for row in bars:
                    row.pop(field)
                    row.update(chart_symbol="NASD100_FUT", price_type="Last")
                c = self.ctx(bars)
                self.assertIsNone(c["sma50"])
                self.assertIsNone(c["sma200"])

    def test_profinance_proof_binds_source_period_and_exact_ohlc(self):
        mutations = (
            lambda b: b["completion_proof"].update(source_identity={"key":"PROFINANCE:Gold"}),
            lambda b: b["completion_proof"].update(source_identity={"key":PF["key"],"contract_id":"wrong"}),
            lambda b: b["completion_proof"].update(period_label="1999-01-01"),
            lambda b: b["completion_proof"].update(successor_label=b["provider_period_label"]),
            lambda b: b["completion_proof"].update(successor_label="1970-01-01"),
            lambda b: b["completion_proof"].update(ohlc_sha256="f"*64),
            lambda b: b.update(close=b["close"]+.2),
            lambda b: b.update(native_time_basis="UTC_SESSION_END"),
            lambda b: b.update(interval_boundary_verified=True),
            lambda b: b.update(native_interval=6),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                bars = fixture()
                mutate(bars[-1])
                self.assertEqual(self.ctx(bars)["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_profinance_actual_first_proof_not_nominal_end_controls_startup(self):
        bars = fixture()
        first_observed = 240*DAY+7200
        for row in bars:
            certify_profinance(row, first_observed)
        before = self.ctx(bars, first_observed-1)
        self.assertIsNone(before["sma50"])
        self.assertIsNone(before["sma200"])
        self.assertIsNone(before["known_at"])
        current = self.ctx(bars, first_observed)
        self.assertEqual(current["status"], "OK")
        self.assertEqual(current["known_at"], first_observed)
        self.assertEqual(current["daily_asof"], 240*DAY)

    def test_profinance_future_observed_proof_cannot_claim_past_availability(self):
        bars = fixture()
        bars[-1]["completion_proof"]["observed_at"] = 240*DAY+7200
        self.assertEqual(self.ctx(bars)["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")
        bars[-1]["completion_proof"]["observed_at"] = True
        self.assertEqual(self.ctx(bars)["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_profinance_asof_explicitly_describes_nominal_date_index(self):
        bars = fixture()
        c = self.ctx(bars)
        p = c["provenance"]
        self.assertEqual(c["daily_asof_basis"], "PROVIDER_DATE_LABEL_ONLY")
        self.assertEqual(p["native_time_basis"], "PROVIDER_DATE_LABEL_ONLY")
        self.assertIs(p["interval_boundary_verified"], False)
        self.assertIsNone(p["last_closed_at"])
        self.assertIsNone(p["verified_close_at"])
        self.assertEqual(p["nominal_last_period_end"], c["daily_asof"])
        self.assertEqual(p["latest_completion_proof"], bars[-1]["completion_proof"])
        self.assertEqual(p["completion_proof_count"], 210)
        self.assertEqual(p["completion_proof_count"], p["digest_bar_count"])
        self.assertEqual(len(p["completion_proofs_sha256"]), 64)
        self.assertTrue(DA.validate_provenance(p, PF, c["known_at"]))
        normalized, _ = DA.validated_bars(bars, c["as_of"], source_identity=PF)
        self.assertEqual(normalized[-1]["completion_proof"], bars[-1]["completion_proof"])

    def test_profinance_frozen_provenance_rejects_false_physical_close_or_future_proof(self):
        c = self.ctx()
        mutations = (
            lambda p: p.update(last_closed_at=c["daily_asof"]),
            lambda p: p.update(verified_close_at=c["daily_asof"]),
            lambda p: p.update(interval_boundary_verified=True),
            lambda p: p.update(native_time_basis="VERIFIED_DAILY_END"),
            lambda p: p.update(completion_proof_count=1),
            lambda p: p.update(completion_proofs_sha256="not-a-digest"),
            lambda p: p.update(last_completion_observed_at=c["known_at"]+1),
            lambda p: p.update(nominal_last_period_end=c["known_at"]+1),
            lambda p: p["latest_completion_proof"].update(observed_at=c["known_at"]+1),
            lambda p: p["latest_completion_proof"].update(ohlc_sha256="f"*64),
            lambda p: p["latest_completion_proof"].update(source_identity={"key":"PROFINANCE:Gold"}),
            lambda p: p.update(history_revision_watermark=c["known_at"]+1),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                changed = deepcopy(c["provenance"])
                mutate(changed)
                self.assertFalse(DA.validate_provenance(changed, PF, c["known_at"]))
        self.assertFalse(DA.validate_provenance(c["provenance"], BTC, c["known_at"]))

    def test_changed_completion_proof_changes_digest_without_changing_averages(self):
        bars = fixture()
        baseline = self.ctx(bars)
        certify_profinance(bars[-1], 240*DAY+30)
        actual = self.ctx(bars)
        self.assertEqual(actual["periods"], baseline["periods"])
        self.assertNotEqual(actual["digest"], baseline["digest"])
        self.assertNotEqual(actual["provenance"]["completion_proofs_sha256"],
                            baseline["provenance"]["completion_proofs_sha256"])
        self.assertEqual(actual["known_at"], 240*DAY+30)

    def test_revised_day_cannot_be_dropped_from_an_earlier_fifty_day_snapshot(self):
        bars = fixture()
        revised_at = 240*DAY+7200
        target = bars[-20]
        target["close"] += .2
        target["revision_observed_at"] = revised_at
        certify_profinance(target, revised_at)
        before = self.ctx(bars, revised_at-1)
        self.assertEqual(before["status"], "DAILY_REVISION_NOT_YET_KNOWN")
        for period in (18,50,200):
            self.assertEqual(before["periods"][str(period)]["status"], "DAILY_REVISION_NOT_YET_KNOWN")
            self.assertIsNone(before["sma"+str(period)])
        after = self.ctx(bars, revised_at)
        self.assertEqual(after["status"], "OK")
        self.assertEqual(after["known_at"], revised_at)
        self.assertAlmostEqual(after["sma50"], math.fsum(b["close"] for b in bars[-50:])/50)
        self.assertEqual(after["provenance"]["history_revision_watermark"], revised_at)
        self.assertTrue(DA.validate_provenance(after["provenance"], PF, after["known_at"]))

    def test_revision_does_not_create_indefinite_future_window_taint(self):
        bars = fixture()
        revised_at = 240*DAY+100
        bars[10]["revision_observed_at"] = revised_at
        certify_profinance(bars[10], revised_at)
        before = self.ctx(bars, revised_at-1)
        self.assertEqual(before["status"], "DAILY_REVISION_NOT_YET_KNOWN")
        after = self.ctx(bars, revised_at+1)
        self.assertEqual(after["periods"]["50"]["status"], "OK")
        self.assertEqual(after["periods"]["200"]["status"], "OK")
        self.assertEqual(after["diagnostics"]["tainted_timestamps"], [])
        self.assertEqual(after["provenance"]["history_revision_watermark"], revised_at)
        self.assertEqual(after["known_at"], revised_at)
        self.assertTrue(DA.validate_provenance(after["provenance"], PF, after["known_at"]))

    def test_foreign_revision_metadata_cannot_rewrite_selected_source_history(self):
        bars = fixture()
        foreign = deepcopy(bars[1])
        foreign["source_identity"] = {"key":"YAHOO:NQ=F"}
        foreign["revision_observed_at"] = 245*DAY
        foreign["available_at"] = 245*DAY
        actual = self.ctx(bars+[foreign])
        self.assertEqual(actual["status"], "OK")
        self.assertNotIn("history_revision_watermark", actual)

    def test_conflicting_completion_proofs_are_not_silently_deduplicated(self):
        bars = fixture()
        bars[-1]["available_at"] += 60
        duplicate = deepcopy(bars[-1])
        duplicate["completion_proof"]["observed_at"] += 1
        context = self.ctx(bars+[duplicate])
        self.assertEqual(context["diagnostics"]["conflicting_timestamps"], 1)
        self.assertEqual(context["periods"]["50"]["status"], "INVALID_DAILY_WINDOW")

    def test_profinance_provenance_return_does_not_alias_caller_proof(self):
        bars = fixture()
        context = self.ctx(bars)
        original = deepcopy(bars[-1]["completion_proof"])
        context["provenance"]["latest_completion_proof"]["source_identity"]["key"] = "changed"
        self.assertEqual(bars[-1]["completion_proof"], original)


if __name__ == "__main__":
    unittest.main()
