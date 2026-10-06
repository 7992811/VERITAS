"""Provider protocol and bounded source-locked D1-cache regressions."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest

import veritas_daily_averages as DA
import veritas_native_daily as ND
import veritas_price_source as VPS
from test_veritas_daily_averages import fixture, DAY, certify_profinance

RAW_BTC = {"asset":"BTC", "source_names":{"primary":"Binance spot"}}
RAW_PF = {"asset":"NQ", "source_names":{"primary":"ProFinance NASD100_FUT"}}


def klines(count=240):
    return [[i*DAY*1000, str(100+i*.1), str(101+i*.1), str(99+i*.1),
             str(100+i*.1), "10", (i+1)*DAY*1000-1] for i in range(count)]


def moex_payload(start=0, count=240):
    rows=[]
    origin=datetime(2025,1,1)
    for i in range(start, start+count):
        date=(origin+timedelta(days=i)).date().isoformat()
        rows.append([100+i*.1, 101+i*.1, 99+i*.1, 100+i*.1, 100,
                     date+" 00:00:00", date+" 23:59:59"])
    return {"candles":{"columns":["open","high","low","close","volume","begin","end"],"data":rows}}


class NativeDailyProviderTests(unittest.TestCase):
    def test_binance_native_utc_daily_parser(self):
        identity=VPS.identity("BTC",RAW_BTC)
        bars=ND.parse_binance(klines(), identity, 240*DAY+1)
        self.assertEqual(len(bars),240)
        self.assertEqual(bars[-1]["end_ts"],240*DAY)
        self.assertEqual(bars[-1]["native_interval"],"1d")
        self.assertEqual(bars[-1]["source_identity"],identity)

    def test_binance_current_day_is_excluded(self):
        bars=ND.parse_binance(klines(241),VPS.identity("BTC",RAW_BTC),240*DAY+1)
        self.assertEqual(len(bars),240)

    def test_binance_hourly_duration_cannot_be_relabelled_daily(self):
        rows=klines()
        rows[-1][6]=rows[-1][0]+3600*1000-1
        with self.assertRaisesRegex(ValueError,"INTERVAL_MISMATCH"):
            ND.parse_binance(rows,VPS.identity("BTC",RAW_BTC),240*DAY+1)

    def test_binance_get_uses_same_spot_series_and_bounded_parameters(self):
        calls=[]
        def fetch(url,params,remaining):
            calls.append((url,params,remaining))
            return klines()
        cache=ND.DailyHistoryCache(fetch_json=fetch,clock=lambda:240*DAY+1)
        r=cache.fetch(RAW_BTC,240*DAY+1)
        self.assertEqual(r["status"],"READY")
        self.assertEqual(calls[0][0],"https://api.binance.com/api/v3/klines")
        self.assertEqual(calls[0][1]["symbol"],"BTCUSDT")
        self.assertEqual(calls[0][1]["interval"],"1d")
        self.assertEqual(calls[0][1]["limit"],500)
        self.assertLessEqual(calls[0][2],4.01)

    def test_all_timeframes_share_one_daily_cache(self):
        calls=[]
        cache=ND.DailyHistoryCache(fetch_json=lambda *a:(calls.append(a) or klines()),
                                  clock=lambda:240*DAY+1)
        for _ in range(7):
            self.assertEqual(cache.fetch(RAW_BTC,240*DAY+1)["status"],"READY")
        self.assertEqual(len(calls),1)

    def test_futures_label_cannot_receive_cached_spot_history(self):
        calls=[]
        cache=ND.DailyHistoryCache(fetch_json=lambda *a:(calls.append(a) or klines()),
                                  clock=lambda:240*DAY+1)
        cache.fetch(RAW_BTC,240*DAY+1)
        wrong={"asset":"BTC","source_names":{"primary":"Binance futures"}}
        r=cache.fetch(wrong,240*DAY+1)
        self.assertEqual(r["reason"],"BINANCE_SPOT_SOURCE_REQUIRED")
        self.assertEqual(r["bars"],[])
        self.assertEqual(len(calls),1)

    def test_failure_keeps_original_cache_time_and_is_rate_limited(self):
        clock=[240*DAY+1]
        calls=[]
        def fetch(*args):
            calls.append(args)
            if len(calls)>1:
                raise RuntimeError("sensitive response body must not be exposed")
            return klines()
        cache=ND.DailyHistoryCache(fetch_json=fetch,clock=lambda:clock[0])
        first=cache.fetch(RAW_BTC,clock[0])
        clock[0]+=1000
        failed=cache.fetch(RAW_BTC,clock[0])
        self.assertEqual(failed["fetched_at"],first["fetched_at"])
        self.assertEqual(failed["reason"],"RuntimeError")
        self.assertTrue(failed["cache_reused"])
        cache.fetch(RAW_BTC,clock[0]+1)
        self.assertEqual(len(calls),2)

    def test_failed_cold_request_does_not_switch_source(self):
        calls=[]
        def fail(*args):
            calls.append(args)
            raise RuntimeError("unavailable")
        cache=ND.DailyHistoryCache(fetch_json=fail,clock=lambda:240*DAY+1)
        for _ in range(4):
            r=cache.fetch(RAW_BTC,240*DAY+1)
            self.assertEqual(r["bars"],[])
        self.assertEqual(len(calls),1)

    def test_zero_budget_makes_no_public_request(self):
        calls=[]
        cache=ND.DailyHistoryCache(fetch_json=lambda *a:(calls.append(a) or klines()),
                                  clock=lambda:240*DAY+1)
        r=cache.fetch(RAW_BTC,240*DAY+1,budget_seconds=0)
        self.assertEqual(r["bars"],[])
        self.assertEqual(calls,[])

    def test_profinance_reuses_native_daily_mapping_without_network(self):
        raw=deepcopy(RAW_PF)
        identity=VPS.identity("NQ",raw)
        rows=fixture(identity=identity)
        for b in rows:
            b.update(chart_symbol="NASD100_FUT",price_type="Last")
        raw["structure_bars_by_timeframe"]={"1d":rows}
        def forbidden(**kwargs):
            self.fail("same native D1 was already attached")
        r=ND.DailyHistoryCache(clock=lambda:240*DAY+1,profinance_fetch=forbidden).fetch(raw,240*DAY+1)
        self.assertEqual(r["status"],"READY")
        self.assertEqual(len(r["bars"]),240)

    def test_explicit_failed_profinance_bundle_cannot_trigger_second_fetch(self):
        raw=deepcopy(RAW_PF)
        raw["native_source_history_attached"]=True
        raw["structure_bars_by_timeframe"]={"1d":[]}
        calls=[]
        def forbidden_fetch(**kwargs):
            calls.append(kwargs)
            raise AssertionError("shared native bundle was already fetched")
        cache=ND.DailyHistoryCache(clock=lambda:240*DAY+1,profinance_fetch=forbidden_fetch)
        r=cache.fetch(raw,240*DAY+1)
        self.assertEqual(calls,[])
        self.assertEqual(r["bars"],[])
        self.assertEqual(r["status"],"UNAVAILABLE")
        self.assertEqual(r["reason"],"NATIVE_DAILY_NO_VALID_CLOSED_BARS")

    def test_profinance_aggregated_hourly_day_is_not_native(self):
        raw=deepcopy(RAW_PF)
        raw["structure_bars_by_timeframe"]={"1d":fixture()}
        for row in raw["structure_bars_by_timeframe"]["1d"]:
            row["aggregation"]="COMPLETE_OBSERVED_BUCKET"
        calls=[]
        def fetch(**kwargs):
            calls.append(kwargs)
            return {"source_identity":VPS.identity("NQ",raw),"bars_by_timeframe":{"1d":[]}}
        r=ND.DailyHistoryCache(clock=lambda:240*DAY+1,profinance_fetch=fetch).fetch(raw,240*DAY+1)
        self.assertEqual(r["bars"],[])
        self.assertEqual(calls[0]["timeframes"],("1d",))

    def test_moex_native_end_is_next_moscow_midnight(self):
        raw={"asset":"MOEX","source_names":{"primary":"MOEX ISS IMOEX"}}
        identity=VPS.identity("MOEX",raw)
        payload=moex_payload(count=2)
        now=ND._moscow_time("2025-01-03 00:00:00")
        bars=ND.parse_moex(payload,identity,now)
        self.assertEqual(len(bars),2)
        self.assertEqual(bars[-1]["end_ts"],now)
        self.assertEqual(bars[-1]["native_interval"],24)

    def test_moex_current_date_remains_partial_even_if_end_is_last_trade(self):
        raw={"asset":"MOEX","source_names":{"primary":"MOEX ISS IMOEX"}}
        payload=moex_payload(count=2)
        payload["candles"]["data"][-1][-1]="2025-01-02 10:00:00"
        now=ND._moscow_time("2025-01-02 11:00:00")
        bars=ND.parse_moex(payload,VPS.identity("MOEX",raw),now)
        self.assertEqual(len(bars),1)

    def test_moex_get_paginates_only_same_security_and_daily_interval(self):
        raw={"asset":"MOEX","source_names":{"primary":"MOEX ISS IMOEX"}}
        calls=[]
        now=ND._moscow_time("2025-08-29 10:00:00")
        def fetch(url,params,remaining):
            calls.append((url,params))
            start=params["start"]
            return moex_payload(start, min(100,240-start))
        r=ND.DailyHistoryCache(fetch_json=fetch,clock=lambda:now).fetch(raw,now)
        self.assertEqual(len(r["bars"]),240)
        self.assertEqual([p["start"] for u,p in calls],[0,100,200])
        self.assertTrue(all(p["interval"]==24 and "/IMOEX/" in u for u,p in calls))

    def test_broker_reuses_exact_uid_daily_snapshot_without_http(self):
        raw={"asset":"CNYRUBF","source_names":{"primary":"TBANK_GRPC CNYRUBF"},
             "contract":{"instrument_uid":"uid-1","secid":"CNYRUBF","normalization_factor":1.}}
        identity=VPS.identity("CNYRUBF",raw)
        native=fixture(identity=identity)
        candles=[{"time":datetime.fromtimestamp(b["ts"],ND.timezone.utc).isoformat(),
                  **{k:b[k] for k in ("open","high","low","close")},"volume_lots":10}
                 for b in native]
        snapshot={"instrument_uid":"uid-1","interval":"1d","aggregation":"NATIVE",
                  "source":"TBANK_GRPC","status":"OK","candles":candles}
        calls=[]
        def snap(asset,tf):
            calls.append((asset,tf))
            return snapshot
        def no_http(*args):
            self.fail("broker native snapshot must not initiate HTTP")
        cache=ND.DailyHistoryCache(fetch_json=no_http,tbank_snapshot=snap,clock=lambda:240*DAY+1)
        r=cache.fetch(raw,240*DAY+1)
        self.assertEqual(r["status"],"READY")
        self.assertEqual(calls,[("CNYRUBF","1d")])
        self.assertEqual(r["bars"][-1]["source_identity"]["contract_id"],"uid-1")

    def test_broker_wrong_contract_or_aggregation_rejected(self):
        raw={"asset":"CNYRUBF","source_names":{"primary":"TBANK_GRPC CNYRUBF"},
             "contract":{"instrument_uid":"uid-1","normalization_factor":1.}}
        identity=VPS.identity("CNYRUBF",raw)
        for snapshot in (
            {"instrument_uid":"uid-2","interval":"1d","aggregation":"NATIVE","source":"TBANK_GRPC","status":"OK"},
            {"instrument_uid":"uid-1","interval":"1d","aggregation":"DAILY_3_CALENDAR_DAYS_UTC_EPOCH","source":"TBANK_GRPC","status":"OK"}):
            with self.assertRaisesRegex(ValueError,"SOURCE_CONTRACT_OR_INTERVAL_MISMATCH"):
                ND.parse_tbank(snapshot,raw,identity,240*DAY+1)

    def test_no_authoritative_identity_means_no_request(self):
        calls=[]
        cache=ND.DailyHistoryCache(fetch_json=lambda *a:calls.append(a))
        r=cache.fetch({"asset":"NQ"},240*DAY+1)
        self.assertEqual(r["bars"],[])
        self.assertEqual(calls,[])

    def test_snapshot_return_is_copied(self):
        cache=ND.DailyHistoryCache(fetch_json=lambda *a:klines(),clock=lambda:240*DAY+1)
        first=cache.fetch(RAW_BTC,240*DAY+1)
        first["bars"][-1]["close"]=1.
        self.assertNotEqual(cache.fetch(RAW_BTC,240*DAY+1)["bars"][-1]["close"],1.)


    def test_profinance_legacy_rows_without_successor_proof_are_unavailable(self):
        raw=deepcopy(RAW_PF)
        rows=fixture(identity=VPS.identity("NQ",raw))
        for row in rows:
            row.pop("completion_proof")
        raw["native_source_history_attached"]=True
        raw["structure_bars_by_timeframe"]={"1d":rows}
        calls=[]
        cache=ND.DailyHistoryCache(clock=lambda:240*DAY+1,profinance_fetch=lambda **k:calls.append(k))
        result=cache.fetch(raw,240*DAY+1)
        self.assertEqual(calls,[])
        self.assertEqual(result["bars"],[])
        self.assertNotEqual(result["status"],"READY")

    def test_profinance_certified_future_proof_is_preserved_but_not_ready(self):
        raw=deepcopy(RAW_PF)
        rows=fixture(identity=VPS.identity("NQ",raw))
        first=240*DAY+7200
        for row in rows:
            certify_profinance(row,first)
        raw["native_source_history_attached"]=True
        raw["structure_bars_by_timeframe"]={"1d":rows}
        cache=ND.DailyHistoryCache(clock=lambda:first)
        before=cache.fetch(raw,first-1)
        self.assertNotEqual(before["status"],"READY")
        self.assertEqual(before["bars"][-1]["completion_proof"]["observed_at"],first)
        self.assertIsNone(before["known_at"])
        after=cache.fetch(raw,first)
        self.assertEqual(after["status"],"READY")
        self.assertEqual(after["known_at"],first)
        self.assertIsNone(after["last_closed_at"])
        self.assertEqual(after["daily_asof"],240*DAY)
        self.assertEqual(after["daily_asof_basis"],"PROVIDER_DATE_LABEL_ONLY")

    def test_fresh_supplied_profinance_revision_bypasses_secondary_cache_ttl(self):
        raw=deepcopy(RAW_PF)
        rows=fixture(identity=VPS.identity("NQ",raw))
        raw["native_source_history_attached"]=True
        raw["structure_bars_by_timeframe"]={"1d":rows}
        clock=[240*DAY+1]
        calls=[]
        cache=ND.DailyHistoryCache(clock=lambda:clock[0],profinance_fetch=lambda **k:calls.append(k))
        first=cache.fetch(raw,clock[0])
        self.assertEqual(first["status"],"READY")
        # Within both TTL and retry cooldown, an already fetched source bundle
        # contains a revised daily close and its actual observation watermark.
        clock[0] += 10
        changed=rows[-20]
        changed["close"] += .2
        changed["revision_observed_at"]=clock[0]
        certify_profinance(changed,clock[0])
        revised=cache.fetch(raw,clock[0])
        self.assertEqual(calls,[])
        self.assertEqual(revised["status"],"READY")
        self.assertEqual(revised["known_at"],clock[0])
        self.assertEqual(revised["bars"][-20]["close"],changed["close"])
        historical=cache.fetch(raw,clock[0]-1)
        self.assertEqual(historical["status"],"DAILY_REVISION_NOT_YET_KNOWN")
        self.assertEqual(historical["bars"][-20]["revision_observed_at"],clock[0])

    def test_profinance_changed_bundle_cannot_refresh_a_proof_implicitly(self):
        raw=deepcopy(RAW_PF)
        rows=fixture(identity=VPS.identity("NQ",raw))
        raw["native_source_history_attached"]=True
        raw["structure_bars_by_timeframe"]={"1d":rows}
        clock=[240*DAY+1]
        cache=ND.DailyHistoryCache(clock=lambda:clock[0])
        first=cache.fetch(raw,clock[0])
        clock[0] += 10
        rows[-1]["close"] += .2
        # No matching source/OHLC proof: the malformed latest row taints SMA50.
        changed=cache.fetch(raw,clock[0])
        self.assertNotEqual(changed["status"],"READY")
        self.assertEqual(changed["bars"][-1]["completion_proof"],
                         first["bars"][-1]["completion_proof"])


    def test_explicit_daily_evidence_preserves_revision_filtered_out_of_structure(self):
        raw=deepcopy(RAW_PF)
        rows=fixture(identity=VPS.identity("NQ",raw))
        raw["native_source_history_attached"]=True
        raw["structure_bars_by_timeframe"]={"1d":deepcopy(rows)}
        observed=240*DAY+7200
        target=rows[-20]
        target["close"] += .2
        target["revision_observed_at"]=observed
        certify_profinance(target,observed)
        raw["native_daily_evidence"]=rows
        calls=[]
        cache=ND.DailyHistoryCache(clock=lambda:observed,profinance_fetch=lambda **k:calls.append(k))
        historical=cache.fetch(raw,observed-1)
        self.assertEqual(calls,[])
        self.assertEqual(historical["status"],"DAILY_REVISION_NOT_YET_KNOWN")
        self.assertEqual(historical["bars"][-20]["revision_observed_at"],observed)
        current=cache.fetch(raw,observed)
        self.assertEqual(current["status"],"READY")
        self.assertEqual(current["known_at"],observed)

    def test_explicit_empty_evidence_overrides_legacy_mapping_without_second_fetch(self):
        raw=deepcopy(RAW_PF)
        raw["native_source_history_attached"]=True
        raw["native_daily_evidence"]=[]
        raw["structure_bars_by_timeframe"]={"1d":fixture(identity=VPS.identity("NQ",raw))}
        calls=[]
        cache=ND.DailyHistoryCache(clock=lambda:240*DAY+1,profinance_fetch=lambda **k:calls.append(k))
        result=cache.fetch(raw,240*DAY+1)
        self.assertEqual(calls,[])
        self.assertEqual(result["bars"],[])
        self.assertEqual(result["reason"],"NATIVE_DAILY_NO_VALID_CLOSED_BARS")

    def test_changed_explicit_evidence_is_ingested_inside_ttl(self):
        raw=deepcopy(RAW_PF)
        rows=fixture(identity=VPS.identity("NQ",raw))
        raw["native_source_history_attached"]=True
        raw["native_daily_evidence"]=rows
        raw["structure_bars_by_timeframe"]={"1d":deepcopy(rows)}
        clock=[240*DAY+1]
        cache=ND.DailyHistoryCache(clock=lambda:clock[0])
        self.assertEqual(cache.fetch(raw,clock[0])["status"],"READY")
        clock[0] += 10
        rows[-20]["close"] += .2
        rows[-20]["revision_observed_at"]=clock[0]
        certify_profinance(rows[-20],clock[0])
        result=cache.fetch(raw,clock[0])
        self.assertEqual(result["bars"][-20]["close"],rows[-20]["close"])
        self.assertEqual(result["known_at"],clock[0])
        self.assertEqual(cache.fetch(raw,clock[0]-1)["status"],"DAILY_REVISION_NOT_YET_KNOWN")


if __name__ == "__main__":
    unittest.main()
