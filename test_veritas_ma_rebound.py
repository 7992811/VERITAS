"""Semantic cases for daily MA50/200 rebounds and shared admission guards."""
from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter
import hashlib
import json
import unittest
from unittest.mock import patch

import veritas_daily_averages as DA
import veritas_ma_rebound as M
import veritas_timeframe_structure as TS

START = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
SOURCE = {"key":"TEST:NQ:EXACT", "asset":"NQ", "contract_id":"NQZ6"}


def daily_history(count=220, closes=None):
    closes = list(closes) if closes is not None else [100.] * count
    count = len(closes)
    return [dict(ts=START-(count-i)*86400, end_ts=START-(count-i-1)*86400,
                 available_at=START-(count-i-1)*86400, open=c, high=c+2, low=c-2,
                 close=c, volume=None, timeframe="1d", native_timeframe="1d",
                 source_identity=deepcopy(SOURCE), finalized=True)
            for i,c in enumerate(closes)]


def local_bar(index, timeframe="5m", o=101.8, h=102.2, l=101.4, c=101.8):
    step = TS.timeframe_seconds(timeframe)
    return dict(ts=START+index*step, available_at=START+(index+1)*step,
                open=o, high=h, low=l, close=c, volume=None,
                timeframe=timeframe, source_identity=deepcopy(SOURCE), finalized=True)


def example(period=50, short=False, timeframe="5m", daily=None):
    daily = deepcopy(daily) if daily is not None else daily_history()
    snapshot = DA.build_context(daily, START, asset="NQ", source_identity=SOURCE, periods=(period,))
    center = (snapshot.get("periods") or {}).get(str(period), {}).get("value") or 100.
    rows = [local_bar(i, timeframe) for i in range(26)]
    rows += [local_bar(26, timeframe, 101.8, 101.9, 99.8, 100.8),
             local_bar(27, timeframe, 100.8, 102.15, 100.6, 102.)]
    for b in rows:
        if short:
            b.update(open=200-b["open"], high=200-b["low"],
                     low=200-b["high"], close=200-b["close"])
        for k in ("open","high","low","close"):
            b[k] += center - 100.
    # Native daily closes continue to arrive during a multi-day intraday walk.
    # They are real timestamped D1 observations, not hourly blocks relabelled D1.
    if timeframe in ("1h","4h"):
        days = int((rows[-1]["available_at"]-START)//86400)
        for day in range(days):
            daily.append(dict(daily[-1],ts=START+day*86400,end_ts=START+(day+1)*86400,
                              available_at=START+(day+1)*86400,open=100.,high=102.,low=98.,close=100.))
    return rows, daily, rows[-1]["available_at"]


def build(rows, daily, now, period=50, timeframe="5m", **kwargs):
    return M.build_context(rows, timeframe, now, daily_bars=daily, asset="NQ",
                           source_identity=SOURCE, ma_config={"periods":(period,)}, **kwargs)


def reference_daily_context_builder(daily_bars, **kwargs):
    """Unindexed baseline: original full validation on each snapshot request."""
    return lambda now: DA.build_context(daily_bars, now, **kwargs)


def reference_build(*args, **kwargs):
    with patch.object(DA, "context_builder", side_effect=reference_daily_context_builder):
        return build(*args, **kwargs)


PF_SOURCE = {"key":"PROFINANCE:NASD100_FUT", "asset":"NQ", "contract_id":"NASD100_FUT"}


def pf_example(proof_at=None, period=50):
    """Native PF date labels acquire completion evidence only when observed."""
    rows,daily,now = example(period=period)
    proof_at = START+20*300 if proof_at is None else proof_at
    daily = deepcopy(daily)
    for b in rows:
        b["source_identity"] = deepcopy(PF_SOURCE)
    for b in daily:
        period_label = datetime.fromtimestamp(b["ts"],timezone.utc).date().isoformat()
        successor_label = datetime.fromtimestamp(b["end_ts"],timezone.utc).date().isoformat()
        digest = hashlib.sha256(json.dumps(
            [float(b[k]) for k in ("open","high","low","close")],
            separators=(",",":"),allow_nan=False).encode()).hexdigest()
        b["ts"] -= 3*3600
        b["end_ts"] -= 3*3600
        b.update(source_identity=deepcopy(PF_SOURCE),native_interval=9,
                 chart_symbol="NASD100_FUT",price_type="Last",
                 provider_period_label=period_label,
                 native_time_basis="PROVIDER_DATE_LABEL_ONLY",
                 interval_boundary_verified=False,
                 available_at=max(b["end_ts"],proof_at),observed_at=proof_at,
                 completion_proof={
                     "kind":"NEXT_NATIVE_DAILY_OBSERVED",
                     "period_label":period_label,"successor_label":successor_label,
                     "observed_at":proof_at,
                     "source_identity":{"key":PF_SOURCE["key"],
                                        "contract_id":PF_SOURCE["contract_id"]},
                     "ohlc_sha256":digest})
    return rows,daily,now


def pf_build(rows, daily, now, period=50):
    return M.build_context(rows,"5m",now,daily_bars=daily,asset="NQ",
                           source_identity=PF_SOURCE,ma_config={"periods":(period,)})


class DailyMAReboundTests(unittest.TestCase):
    def test_50_and_200_both_directions_need_local_closed_confirmation(self):
        for period in (50,200):
            for short in (False,True):
                with self.subTest(period=period,short=short):
                    rows,daily,now = example(period,short)
                    self.assertIsNone(build(rows[:-1],daily,now,period)["event"])
                    result = build(rows,daily,now,period)
                    e = result["event"]
                    self.assertIsNotNone(e,result)
                    self.assertEqual(e["event_type"],"DAILY_MA_REBOUND")
                    self.assertEqual(e["ma_proof"]["period"],period)
                    self.assertEqual(e["direction"],"SHORT" if short else "LONG")
                    self.assertEqual(e["signal_at"],now)
                    self.assertTrue(M.validate_event(e,SOURCE)["eligible"])
                    self.assertTrue(TS.entry_gate(result,e["signal_price"],e["direction"],now)["eligible"])
                    self.assertEqual({e[k] for k in ("timeframe","atr_timeframe","stop_timeframe","target_timeframe")},{"5m"})
                    self.assertFalse(e["volume_observed"])
                    self.assertIsNone(e["relative_volume"])
                    self.assertLessEqual(e["ma_proof"]["daily_known_at"],rows[-2]["ts"])
                    self.assertLessEqual(e["stop_level_available_at"],rows[-1]["ts"])
                    self.assertLessEqual(e["atr_observed_until"],rows[-1]["ts"])

    def test_stop_target_use_local_atr_and_do_not_rebase_to_quote(self):
        rows,daily,now = example()
        result = build(rows,daily,now)
        e = result["event"]
        self.assertAlmostEqual(e["atr"],.865)
        self.assertAlmostEqual(e["stop_price"],99.8-.15*.865)
        self.assertAlmostEqual(e["target_price"],101.9+2*(101.9-e["stop_price"]))
        self.assertNotEqual(e["atr"],e["ma_proof"]["daily_atr"])
        before = deepcopy(e)
        for quote in (101.92,102.0):
            TS.entry_gate(result,quote,"LONG",now)
        self.assertEqual(e,before)

    def test_intraday_timeframes_do_not_wait_for_new_daily_close(self):
        for timeframe in ("1m","5m","1h","4h"):
            with self.subTest(timeframe=timeframe):
                rows,daily,now = example(timeframe=timeframe)
                e = build(rows,daily,now,timeframe=timeframe)["event"]
                self.assertIsNotNone(e)
                self.assertEqual(e["timeframe"],timeframe)
                self.assertEqual(e["signal_at"],now)
                expected_daily = START+int((rows[-2]["ts"]-START)//86400)*86400
                self.assertEqual(e["ma_proof"]["daily_asof"],expected_daily)
        rows,daily,now = example(timeframe="1m")
        self.assertLess(now,START+86400)

    def test_daily_entry_uses_known_prior_daily_ma_and_same_daily_stop(self):
        rows,daily,now = example(timeframe="1d")
        native_rows = [dict(b,native_timeframe="1d",end_ts=b["available_at"]) for b in rows]
        e = build(rows,daily+native_rows,now,timeframe="1d")["event"]
        self.assertIsNotNone(e)
        self.assertEqual(e["timeframe"],"1d")
        self.assertLessEqual(e["ma_proof"]["daily_known_at"],rows[-2]["ts"])
        self.assertEqual(e["ma_proof"]["daily_asof"],rows[-2]["ts"])
        self.assertTrue(M.validate_event(e,SOURCE)["eligible"])

    def test_forming_and_future_local_bars_cannot_confirm(self):
        rows,daily,now = example()
        self.assertIsNone(build(rows,daily,now-1)["event"])
        for flag in ("finalized","complete","closed"):
            changed = deepcopy(rows)
            changed[-1][flag] = False
            self.assertIsNone(build(changed,daily,now+100)["event"])
        changed = deepcopy(rows)
        changed[-1]["available_at"] = now+300
        self.assertIsNone(build(changed,daily,now)["event"])

    def test_future_daily_candles_and_quote_like_local_rows_do_not_change_event(self):
        rows,daily,now = example()
        baseline = build(rows,daily,now)["event"]
        future = dict(daily[-1],ts=START,end_ts=START+86400,available_at=START+86400,
                      open=100000,high=100002,low=99998,close=100000)
        self.assertEqual(build(rows,daily+[future],now)["event"],baseline)
        extra = local_bar(28,c=150,h=151)
        extra.update(source="DIRECT_QUOTE_ANCHOR",synthetic=True)
        self.assertEqual(build(rows+[extra],daily,now)["event"],baseline)

    def test_daily_availability_after_touch_cannot_supply_past_ma(self):
        rows,daily,now = example(200)
        changed = [dict(b,available_at=now+86400) for b in daily]
        r = build(rows,changed,now,200)
        self.assertIsNone(r["event"])
        self.assertIn("MA_DAILY_HISTORY_REQUIRED",r["diagnostics"])

    def test_missing_ma200_does_not_disable_available_ma50(self):
        rows,daily,now = example(daily=daily_history(70))
        r = M.build_context(rows,"5m",now,daily_bars=daily,asset="NQ",source_identity=SOURCE)
        self.assertIsNotNone(r["event"])
        self.assertEqual(r["event"]["ma_proof"]["period"],50)
        self.assertIsNone(build(rows,daily,now,200)["event"])

    def test_native_daily_and_local_sources_must_match(self):
        rows,daily,now = example()
        wrong = {"key":"FOREIGN:NQ","contract_id":"NQZ6"}
        self.assertIsNone(build(rows,[dict(b,source_identity=wrong) for b in daily],now)["event"])
        changed = deepcopy(rows)
        changed[-2]["source_identity"] = wrong
        self.assertEqual(build(changed,daily,now)["reason"],"MA_LOCAL_SOURCE_MISMATCH")
        relabelled = [dict(b,native_timeframe="1h") for b in daily]
        self.assertIsNone(build(rows,relabelled,now)["event"])

    def test_source_display_label_change_does_not_rename_event(self):
        rows,daily,now = example()
        expected = build(rows,daily,now)["event"]["event_id"]
        alias = dict(SOURCE,primary_source="same stream new label",version="V2")
        changed_rows = [dict(b,source_identity=alias) for b in rows]
        changed_daily = [dict(b,source_identity=alias) for b in daily]
        r = M.build_context(changed_rows,"5m",now,daily_bars=changed_daily,
                            asset="NQ",source_identity=alias,ma_config={"periods":(50,)})
        self.assertEqual(r["event"]["event_id"],expected)

    def test_flat_daily_average_with_repeated_crosses_is_not_an_entry(self):
        rows,daily,now = example(daily=daily_history(closes=[99. if i%2 else 101. for i in range(220)]))
        r = build(rows,daily,now)
        self.assertIsNone(r["event"])
        self.assertIn("MA_FLAT_REPEATED_CROSSES",r["diagnostics"])

    def test_strong_daily_slope_against_direction_blocks(self):
        for short in (False,True):
            closes = [200.-i*.5 for i in range(220)] if not short else [80.+i*.5 for i in range(220)]
            rows,daily,now = example(short=short,daily=daily_history(closes=closes))
            r = build(rows,daily,now)
            self.assertIsNone(r["event"])
            self.assertIn("MA_ADVERSE_DAILY_SLOPE",r["diagnostics"])

    def test_recross_in_same_episode_cannot_refresh_entry_time(self):
        rows,daily,now = example()
        first = build(rows,daily,now)["event"]
        for i in range(28,34):
            rows.append(local_bar(i,o=100.8,h=102.15,l=100.2,c=102.0 if i%2 else 100.8))
        later = build(rows,daily,rows[-1]["available_at"])["event"]
        self.assertEqual(later["event_id"],first["event_id"])
        self.assertEqual(later["signal_at"],first["signal_at"])
        self.assertEqual(later["stop_price"],first["stop_price"])
        self.assertEqual(later["target_price"],first["target_price"])

    def test_new_clear_excursion_then_new_touch_can_start_independent_episode(self):
        rows,daily,now = example()
        first = build(rows,daily,now)["event"]
        rows += [local_bar(i) for i in range(28,31)]
        rows += [local_bar(31,o=101.8,h=101.9,l=99.8,c=100.8),
                 local_bar(32,o=100.8,h=102.15,l=100.6,c=102.)]
        new = build(rows,daily,rows[-1]["available_at"])["event"]
        self.assertNotEqual(new["event_id"],first["event_id"])
        self.assertEqual(new["ma_proof"]["episode_start_at"],rows[-2]["ts"])
        self.assertEqual(new["signal_at"],rows[-1]["available_at"])

    def test_old_unconfirmed_episode_cannot_confirm_after_deadline(self):
        rows,daily,now = example()
        rows = rows[:-1]
        for i in range(27,41):
            rows.append(local_bar(i,o=100.8,h=101.8,l=100.2,c=100.8))
        rows.append(local_bar(41,o=100.8,h=102.15,l=100.6,c=102.))
        r = build(rows,daily,rows[-1]["available_at"])
        self.assertIsNone(r["event"])
        self.assertIn("MA_EPISODE_EXPIRED",r["diagnostics"])

    def test_gap_cannot_turn_an_old_touch_into_a_fresh_episode(self):
        rows,daily,now = example()
        confirming = dict(rows[-1])
        confirming["ts"] += 86400
        confirming["available_at"] += 86400
        r = build(rows[:-1]+[confirming],daily,confirming["available_at"])
        self.assertIsNone(r["event"])
        self.assertIn("MA_EPISODE_EXPIRED",r["diagnostics"])

    def test_spent_target_remains_spent_when_price_returns(self):
        rows,daily,now = example()
        first = build(rows,daily,now)["event"]
        target = first["target_price"]
        rows += [local_bar(28,o=102.,h=target+.1,l=101.5,c=target),
                 local_bar(29,o=target,h=target+.1,l=100.5,c=101.9)]
        r = build(rows,daily,rows[-1]["available_at"])
        self.assertEqual(r["event"]["event_id"],first["event_id"])
        self.assertTrue(r["event"]["spent"])
        self.assertEqual(r["event"]["spent_reason"],"SAME_TF_TARGET_ALREADY_REACHED")
        self.assertFalse(TS.entry_gate(r,101.9,"LONG",rows[-1]["available_at"])["eligible"])

    def test_common_age_extension_and_stop_distance_guards_are_not_bypassed(self):
        rows,daily,now = example()
        r = build(rows,daily,now)
        e = r["event"]
        self.assertEqual(TS.entry_gate(r,102.,"LONG",now+301)["reason"],"SAME_TF_EVENT_EXPIRED")
        quote = e["trigger_level"]+.501*e["atr"]
        self.assertEqual(TS.entry_gate(r,quote,"LONG",now)["reason"],"SAME_TF_ENTRY_EXTENDED")
        self.assertEqual(TS.entry_gate(r,102.,"LONG",now,config={"max_stop_atr":2.})["reason"],"SAME_TF_STOP_TOO_DISTANT")

    def test_proof_rejects_rebased_time_foreign_daily_data_and_geometry(self):
        rows,daily,now = example()
        original = build(rows,daily,now)["event"]
        mutations = [
            lambda e:e["ma_proof"].update(daily_known_at=now),
            lambda e:e["ma_proof"].update(daily_valid_until=START-1),
            lambda e:e["ma_proof"].update(ma_value=80.),
            lambda e:e["ma_proof"]["daily_provenance"].update(source_identity={"key":"OTHER"}),
            lambda e:e["ma_proof"]["period_evidence"].update(sample_count=49),
            lambda e:e.update(signal_at=now+300),
            lambda e:e.update(stop_price=e["stop_price"]+.5),
            lambda e:e.update(target_price=e["target_price"]+10),
            lambda e:e.update(stop_level_available_at=now+1),
            lambda e:e.update(atr_timeframe="1d"),
            lambda e:e.update(event_id="MAR_REBASED"),
        ]
        for mutate in mutations:
            e = deepcopy(original)
            mutate(e)
            self.assertFalse(M.validate_event(e,SOURCE)["eligible"])

    def test_unknown_volume_does_not_become_observed_and_inputs_are_immutable(self):
        rows,daily,now = example()
        rows = [dict(b,volume=100.,volume_available=False) for b in rows]
        before = deepcopy((rows,daily))
        e = build(rows,daily,now)["event"]
        self.assertFalse(e["volume_observed"])
        self.assertIsNone(e["relative_volume"])
        self.assertEqual((rows,daily),before)

    def test_short_native_daily_session_changes_cache_at_actual_end(self):
        source = {"key":"MOEX:CNYRUBF", "asset":"CNYRUBF", "contract_id":"CNYRUBF"}
        # Native MOEX interval24 may cover a15-hour exchange session. The
        # provider's inclusive23:59:59 end becomes next00:00 in closed bars.
        daily = [dict(b, ts=b["ts"]+9*3600, native_interval=24,
                      provider_end_ts=b["end_ts"]-1,
                      source_identity=deepcopy(source)) for b in daily_history()]
        rows,_,_ = example()
        for b in rows:
            b["ts"] += 22*3600
            b["available_at"] += 22*3600
            b["source_identity"] = deepcopy(source)
        daily.append(dict(daily[-1], ts=START+9*3600, end_ts=START+86400,
                          provider_end_ts=START+86400-1,
                          available_at=START+86400))
        r = M.build_context(rows, "5m", rows[-1]["available_at"],
                            daily_bars=daily, asset="CNYRUBF", source_identity=source,
                            ma_config={"periods":(50,)})
        self.assertIsNotNone(r["event"],r)
        self.assertEqual(r["daily_snapshot_builds"],2)
        proof = r["event"]["ma_proof"]
        self.assertEqual(proof["daily_asof"],START+86400)
        self.assertLessEqual(proof["daily_known_at"],proof["episode_start_at"])
        self.assertTrue(M.validate_event(r["event"],source)["eligible"])

    def test_cached_daily_snapshot_expires_before_new_touch_without_daily_sort(self):
        rows,daily,now = example()
        # Cache starts while the daily prefix is fresh; touch arrives five days
        # later with exactly the same prefix. Cached OK must not remain eligible.
        for b in rows[-2:]:
            b["ts"] += 5*86400
            b["available_at"] += 5*86400
        r = build(rows,daily,rows[-1]["available_at"])
        self.assertIsNone(r["event"])
        self.assertIn("MA_DAILY_CONTEXT_STALE",r["diagnostics"])
        self.assertEqual(r["daily_snapshot_builds"],1)

    def test_daily_snapshots_are_cached_within_intraday_scan(self):
        rows,daily,now = example(timeframe="1m")
        with patch.object(DA,"context_builder",wraps=DA.context_builder) as observed:
            r = build(rows,daily,now,timeframe="1m")
        self.assertIsNotNone(r["event"])
        self.assertEqual(observed.call_count,1)
        self.assertEqual(r["daily_snapshot_builds"],1)


    def test_indexed_daily_snapshots_preserve_complete_context_on_every_timeframe(self):
        for timeframe in ("1m","5m","1h","4h","1d"):
            for period in (50,200):
                for short in (False,True):
                    with self.subTest(timeframe=timeframe,period=period,short=short):
                        rows,daily,now = example(period,short,timeframe)
                        if timeframe == "1d":
                            daily += [dict(b,native_timeframe="1d",end_ts=b["available_at"])
                                      for b in rows]
                        optimized = build(rows,daily,now,period,timeframe)
                        reference = reference_build(rows,daily,now,period,timeframe)
                        self.assertEqual(optimized,reference)
                        self.assertIsNotNone(optimized["event"])

    def test_indexed_daily_snapshots_preserve_consumed_spent_and_independent_episodes(self):
        rows,daily,now = example()
        first = build(rows,daily,now)["event"]
        # Repeated touch/recross stays the original episode.
        for i in range(28,34):
            rows.append(local_bar(i,o=100.8,h=102.15,l=100.2,c=102. if i%2 else 100.8))
        repeated = build(rows,daily,rows[-1]["available_at"])
        self.assertEqual(repeated,reference_build(rows,daily,rows[-1]["available_at"]))
        self.assertEqual(repeated["event"]["event_id"],first["event_id"])
        # A spent target stays spent after return to the trigger.
        target = first["target_price"]
        rows += [local_bar(34,o=102.,h=target+.1,l=101.5,c=target),
                 local_bar(35,o=target,h=target+.1,l=100.5,c=101.9)]
        spent = build(rows,daily,rows[-1]["available_at"])
        self.assertEqual(spent,reference_build(rows,daily,rows[-1]["available_at"]))
        self.assertTrue(spent["event"]["spent"])
        self.assertEqual(spent["event"]["event_id"],first["event_id"])
        # Only a new independent excursion can rearm.
        rows += [local_bar(i) for i in range(36,39)]
        rows += [local_bar(39,o=101.8,h=101.9,l=99.8,c=100.8),
                 local_bar(40,o=100.8,h=102.15,l=100.6,c=102.)]
        rearmed = build(rows,daily,rows[-1]["available_at"])
        self.assertEqual(rearmed,reference_build(rows,daily,rows[-1]["available_at"]))
        self.assertNotEqual(rearmed["event"]["event_id"],first["event_id"])

    def test_five_hundred_bar_walk_does_not_forget_an_old_consumed_episode(self):
        tail,_,first_now = example(timeframe="1d")
        continued = [local_bar(i,"1d",o=100.8,h=102.15,l=99.8,c=100.8)
                     for i in range(28,280)]
        daily = daily_history(220) + [
            dict(b,native_timeframe="1d",end_ts=b["available_at"])
            for b in tail+continued]
        rows = deepcopy(daily)
        first = build(rows[:248],daily,first_now,timeframe="1d")["event"]
        self.assertIsNotNone(first)
        result = build(rows,daily,rows[-1]["available_at"],timeframe="1d")
        self.assertEqual(result["bars"],500)
        # Hundreds of touches after confirmation cannot erase the consumed
        # episode or manufacture a newly timed confirmation.
        self.assertEqual(result["event"],first)
        self.assertEqual(TS.entry_gate(result,first["signal_price"],"LONG",
                                      rows[-1]["available_at"])["reason"],
                         "SAME_TF_EVENT_EXPIRED")

    def test_five_hundred_daily_bars_keep_all_episodes_with_one_native_validation_pass(self):
        tail,_,now = example(timeframe="1d")
        daily = daily_history(472) + [
            dict(b,native_timeframe="1d",end_ts=b["available_at"]) for b in tail]
        # The local walk is deliberately all500 observations, not a truncated
        # recent tail; history, event ID, proof and diagnostics must stay exact.
        rows = deepcopy(daily)
        with patch.object(DA,"_native",wraps=DA._native) as indexed_native:
            begin = perf_counter()
            optimized = build(rows,daily,now,timeframe="1d")
            indexed_seconds = perf_counter()-begin
        with patch.object(DA,"_native",wraps=DA._native) as reference_native:
            begin = perf_counter()
            reference = reference_build(rows,daily,now,timeframe="1d")
            reference_seconds = perf_counter()-begin
        self.assertEqual(optimized,reference)
        self.assertEqual(optimized["bars"],500)
        self.assertEqual(optimized["daily_snapshot_builds"],479)
        self.assertIsNotNone(optimized["event"])
        self.assertEqual(optimized["event"]["signal_at"],now)
        self.assertLessEqual(indexed_native.call_count,len(daily))
        self.assertGreater(reference_native.call_count,50*len(daily))
        # Report CI timings as measurements, not a machine-dependent pass gate.
        print("MA_D1_500_PERF instrumented_indexed_seconds=%.6f instrumented_reference_seconds=%.6f "
              "indexed_native=%d reference_native=%d" %
              (indexed_seconds,reference_seconds,indexed_native.call_count,
               reference_native.call_count))

    def test_pf_first_observed_completion_cannot_authorize_an_earlier_touch(self):
        proof_at = START+27*300
        rows,daily,now = pf_example(proof_at)
        earlier = pf_build(rows,daily,now)
        self.assertIsNone(earlier["event"])
        self.assertIn("MA_DAILY_HISTORY_REQUIRED",earlier["diagnostics"])
        # Once the daily proof exists, a NEW clear approach/touch/closed
        # confirmation can enter; the earlier touch is never backdated.
        added = [local_bar(i) for i in range(28,31)]
        added += [local_bar(31,o=101.8,h=101.9,l=99.8,c=100.8),
                  local_bar(32,o=100.8,h=102.15,l=100.6,c=102.)]
        rows += [dict(b,source_identity=deepcopy(PF_SOURCE)) for b in added]
        result = pf_build(rows,daily,rows[-1]["available_at"])
        e = result["event"]
        self.assertIsNotNone(e,result)
        self.assertEqual(e["ma_proof"]["episode_start_at"],rows[-2]["ts"])
        self.assertEqual(e["ma_proof"]["daily_known_at"],proof_at)
        self.assertLessEqual(proof_at,e["ma_proof"]["episode_start_at"])
        self.assertTrue(M.validate_event(e,PF_SOURCE)["eligible"])

    def test_pf_event_keeps_date_label_and_completion_proof_without_claiming_close_time(self):
        for period in (50,200):
            with self.subTest(period=period):
                rows,daily,now = pf_example(period=period)
                result = pf_build(rows,daily,now,period)
                e = result["event"]
                self.assertIsNotNone(e,result)
                proof = e["ma_proof"]
                dp = proof["daily_provenance"]
                self.assertEqual(proof["daily_asof_basis"],"PROVIDER_DATE_LABEL_ONLY")
                self.assertEqual(dp["native_time_basis"],"PROVIDER_DATE_LABEL_ONLY")
                self.assertIs(dp["interval_boundary_verified"],False)
                self.assertIsNone(dp["last_closed_at"])
                self.assertIsNone(dp["verified_close_at"])
                self.assertEqual(dp["nominal_last_period_end"],proof["daily_asof"])
                self.assertEqual(dp["last_period_label"],daily[-1]["provider_period_label"])
                self.assertEqual(dp["latest_completion_proof"],daily[-1]["completion_proof"])
                self.assertGreater(dp["completion_proof_count"],0)
                self.assertEqual(len(dp["completion_proofs_sha256"]),64)
                self.assertTrue(M.validate_event(e,PF_SOURCE)["eligible"])

    def test_pf_rereading_unchanged_proof_does_not_refresh_event(self):
        rows,daily,now = pf_example()
        original = pf_build(rows,daily,now)["event"]
        self.assertIsNotNone(original)
        # HTTP observation may advance. The first completion proof and actual
        # candle availability stay pinned for unchanged OHLC.
        reread = [dict(b,observed_at=now+600,fetched_at=now+600) for b in daily]
        refreshed = pf_build(rows,reread,now+600)["event"]
        self.assertEqual(refreshed,original)
        self.assertEqual(refreshed["event_id"],original["event_id"])
        self.assertEqual(refreshed["signal_at"],original["signal_at"])

    def test_pf_event_rejects_forged_completion_source_time_or_session_boundary(self):
        rows,daily,now = pf_example()
        original = pf_build(rows,daily,now)["event"]
        self.assertIsNotNone(original)
        mutations = [
            lambda e:e["ma_proof"].update(daily_asof_basis="VERIFIED_SESSION_CLOSE"),
            lambda e:e["ma_proof"]["daily_provenance"].update(last_closed_at=START),
            lambda e:e["ma_proof"]["daily_provenance"].update(verified_close_at=START),
            lambda e:e["ma_proof"]["daily_provenance"].update(interval_boundary_verified=True),
            lambda e:e["ma_proof"]["daily_provenance"].update(nominal_last_period_end=START+86400),
            lambda e:e["ma_proof"]["daily_provenance"].update(completion_proofs_sha256=""),
            lambda e:e["ma_proof"]["daily_provenance"]["latest_completion_proof"].update(observed_at=now),
            lambda e:e["ma_proof"]["daily_provenance"]["latest_completion_proof"].update(
                source_identity={"key":"PROFINANCE:FOREIGN","contract_id":"OTHER"}),
        ]
        for mutation in mutations:
            changed = deepcopy(original)
            mutation(changed)
            self.assertFalse(M.validate_event(changed,PF_SOURCE)["eligible"])

    def test_invalid_clock_and_unsupported_timeframe_fail_closed(self):
        rows,daily,now = example()
        for bad in (None,True,float("nan"),"2026-10-01T00:00:00"):
            r = build(rows,daily,bad)
            self.assertIsNone(r["event"])
            self.assertEqual(r["reason"],"SAME_TF_DECISION_TIME_REQUIRED")
        self.assertEqual(build(rows,daily,now,timeframe="7d")["reason"],"MA_TIMEFRAME_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
