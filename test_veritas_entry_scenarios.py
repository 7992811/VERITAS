"""End-to-end native MA integration, portfolio policy and partial-feed regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_entry_scenarios as S
import veritas_feature_history as FH
import veritas_timeframe_data as TFD
import veritas_timeframe_policy as TFP
import veritas_timeframe_structure as TS
import veritas_user_teaching as UT
import veritas_price_source as VPS
from test_veritas_timeframe_policy import structural_row


def native_days(clock, identity, value=100., count=220, known_before=None):
    if str(identity.get("key", "")).startswith("PROFINANCE:"):
        import veritas_profinance_history as PF
        proof_clock = known_before or clock
        end = int(proof_clock.timestamp())//86400*86400
        lines = ["0;Open;High;Low;Close;Date"]
        for i in range(count+1):
            label = datetime.fromtimestamp(end-(count-i)*86400, timezone.utc).strftime("%d.%m.%Y")
            lines.append("%s;%s;%s;%s;%s;%s" % (i, value, value+1.5, value-1.5, value, label))
        return PF.parse_history("\n".join(lines), identity["asset"], "1d",
                                now=proof_clock, observed_at=proof_clock)["bars"]
    end = int(clock.timestamp())//86400*86400
    return [dict(ts=end-(count-i)*86400, end_ts=end-(count-i-1)*86400,
                 open=value, high=value+1.5, low=value-1.5, close=value,
                 timeframe="1d", native_timeframe="1d", aggregation="NATIVE",
                 source_identity=deepcopy(identity)) for i in range(count)]


def rebound_raw(clock, timeframe="5m", asset="NQ", direction="LONG"):
    provider = "MOEX ISS CNYRUBF" if asset=="CNYRUBF" else "ProFinance"
    identity = VPS.identity(asset, {"source":provider})
    step = TS.timeframe_seconds(timeframe)
    finish = clock.timestamp()-10
    start = finish-34*step
    rows = [dict(ts=start+i*step, open=102., high=102.5, low=101.5,
                 close=102., volume=None, timeframe=timeframe,
                 source_identity=deepcopy(identity)) for i in range(32)]
    rows.extend([
        dict(ts=start+32*step, open=102., high=102.1, low=99.8, close=100.1,
             volume=None, timeframe=timeframe, source_identity=deepcopy(identity)),
        dict(ts=start+33*step, open=100.1, high=102.3, low=100., close=102.2,
             volume=None, timeframe=timeframe, source_identity=deepcopy(identity))])
    if direction=="SHORT":
        for row in rows:
            row.update(open=200-row["open"], close=200-row["close"],
                       high=200-row["low"], low=200-row["high"])
    return dict(asset=asset, price=rows[-1]["close"], source=provider,
        source_names={"primary":provider}, structure_source_identity=identity,
        structure_bars_by_timeframe={timeframe:rows},
        native_daily_bars=native_days(clock,identity,known_before=clock-timedelta(seconds=40*step)),
        observed_at=clock.isoformat(), market_observed_at=clock.isoformat(),
        source_gate_pass=True, market_open=True, paper_eligible=True,
        direct_sources=1, source_divergence=0.,
        closes=[], highs=[], lows=[], vols=[], taker_buy=[], returns=[],
        binance_close_time_ms=int(clock.timestamp()*1000))


class EntryScenarioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock=datetime.now(timezone.utc).replace(microsecond=0)

    def test_daily_values_ignore_foreign_hourly_groups(self):
        raw=rebound_raw(self.clock)
        raw["closes"]=[777.]*6000
        features=S.daily_features(raw,self.clock)
        self.assertEqual((features["sma18"],features["sma50"],features["sma200"]),(100.,100.,100.))
        self.assertEqual(features["ma_timeframe"],"1d")
        self.assertTrue(features["ma_native_daily"])
        self.assertEqual(features["ma_source_identity"]["key"],raw["structure_source_identity"]["key"])

    def test_missing_sma200_does_not_substitute_shorter_average(self):
        raw=rebound_raw(self.clock)
        raw["native_daily_bars"]=raw["native_daily_bars"][-60:]
        f=S.daily_features(raw,self.clock)
        self.assertEqual(f["sma50"],100.)
        self.assertIsNone(f["sma200"])
        self.assertEqual(f["daily_ma_context"]["periods"]["200"]["status"],"INSUFFICIENT_DAILY_HISTORY")

    def test_foreign_daily_candles_cannot_create_a_level(self):
        raw=rebound_raw(self.clock)
        raw["native_daily_bars"][-1]["source_identity"]=VPS.identity("NQ",{"source":"Yahoo CME NQ=F"})
        self.assertIsNone(S.daily_features(raw,self.clock)["sma50"])

    def test_absent_daily_data_preserves_structural_breakout_and_identity(self):
        row=structural_row(self.clock)
        raw=dict(row,native_daily_bars=[],structure_source_identity=row["timeframe_entry_context"]["source_identity"])
        selected=S.select_context(raw,"5m",self.clock,row["timeframe_entry_context"])
        self.assertEqual(selected["event"],row["timeframe_entry_context"]["event"])
        self.assertTrue(selected["entry_scenarios"][0]["timing_eligible"])
        self.assertEqual(selected["selected_scenario"],"SAME_TIMEFRAME_STRUCTURAL_BREAKOUT")

    def test_scenario_selection_uses_observed_book_for_fill_and_costs(self):
        row=structural_row(self.clock)
        context=row["timeframe_entry_context"]
        plain,_=S._assess(context,row,"5m",self.clock)
        self.assertEqual(plain[0],1)
        quote=dict(row,best_bid=100.8,best_ask=102.,spread_bps=118.)
        rank,evidence=S._assess(context,quote,"5m",self.clock)
        self.assertEqual(rank[0],0,evidence)
        self.assertTrue(evidence["timing_eligible"])
        self.assertFalse(evidence["economics_eligible"])
        self.assertIn("SAME_TF_ENTRY_EXTENDED",evidence["economics_blockers"])
        self.assertGreater(evidence["modeled_round_trip_cost_pct"],.0016)

    def test_native_rebound_reaches_all_five_canonical_portfolios_in_both_directions(self):
        for name in CTC.PORTFOLIO_ORDER:
            for direction in ("LONG","SHORT"):
                tf="1h" if name=="Champion" else "5m"
                asset="CNYRUBF" if name=="Currency" else "NQ"
                raw=rebound_raw(self.clock,tf,asset,direction)
                context=TFD.context(raw,tf,self.clock)
                with self.subTest(portfolio=name,direction=direction):
                    self.assertEqual((context.get("event") or {}).get("event_type"),"DAILY_MA_REBOUND",context)
                    row=dict(raw,horizon=tf,research_decision=direction,decision=direction,
                        confidence=.90, signal_tier="SUPER_"+direction,
                        horizon_structure={"direction":direction,"state":"CONFIRMED_TREND","score":.90},
                        independent_evidence_families=5,
                        _local_execution_context={"same_direction_count":1,"opposite_direction_count":0},
                        timeframe_entry_context=context,trade_plan={"market_observed_at":self.clock.isoformat()})
                    prepared=TFP.prepare_row(row,now=self.clock)
                    self.assertEqual(prepared["trade_plan"]["user_teaching_id"],UT.MA_TEACHING_ID)
                    decision=VCR.evaluate(prepared,CTC.runtime_portfolio_policy(name),0.,self.clock)
                    self.assertTrue(decision["open"],decision)
                    self.assertEqual(decision["prepared_plan"]["stop_timeframe"],tf)
                    self.assertEqual(decision["prepared_plan"]["atr_timeframe"],tf)
                    self.assertTrue(UT.verify_entry_trace(UT.entry_trace(context,name)))

    def test_attached_bundle_cannot_replace_its_original_source_identity(self):
        raw=rebound_raw(self.clock)
        raw.update(native_source_history_attached=True,
                   structure_source_identity=VPS.identity("BRENT",{"source":"MOEX ISS BRX6","contract_id":"BRX6"}))
        with patch("veritas_profinance_history.fetch_history_bundle") as remote:
            attached=TFD.attach(raw,self.clock)
        remote.assert_not_called()
        self.assertEqual(attached["structure_history_error"],"SAME_TF_SOURCE_MISMATCH")
        self.assertEqual(attached["structure_bars_by_timeframe"],{})

    def test_refresh_keeps_rebound_id_and_cannot_revive_expired_signal(self):
        raw=rebound_raw(self.clock)
        first=TFD.context(raw,"5m",self.clock)
        self.assertEqual((first.get("event") or {}).get("event_type"),"DAILY_MA_REBOUND",first)
        later=self.clock+timedelta(seconds=301)
        second=TFD.context(raw,"5m",later)
        self.assertEqual(second["event"]["event_id"],first["event"]["event_id"])
        gate=TS.entry_gate(second,raw["price"],"LONG",later,CTC.STRUCTURAL_ENTRY_POLICY)
        self.assertEqual(gate["reason"],"SAME_TF_EVENT_EXPIRED")

    def test_fresh_trigger_preserves_confirmed_aligned_trend(self):
        old={"horizon_structure":{"direction":"LONG","state":"CONFIRMED_TREND","score":.85}}
        new=S.confirmed_structure(old,"LONG",.70,"1h")
        self.assertEqual(new["state"],"CONFIRMED_TREND")
        self.assertEqual(new["score"],.85)
        self.assertEqual(S.confirmed_structure(old,"SHORT",.70,"1h")["state"],"BUILDING_TREND")
        self.assertEqual(old["horizon_structure"]["direction"],"LONG")

    def test_owner_records_are_idempotent_and_keep_original_snapshot_hash(self):
        ledger={}
        def write(kind,key,payload,*args):
            created=key not in ledger
            ledger.setdefault(key,deepcopy(payload))
            return created
        def read(kind,key):
            return {"payload":ledger.get(key)}
        first=UT.seed_all_user_teachings(write,read)
        second=UT.seed_all_user_teachings(write,read)
        self.assertEqual(set(ledger),{UT.TEACHING_ID,UT.MA_TEACHING_ID,UT.BREAKOUT_TEACHING_ID,UT.ACCELERATION_TEACHING_ID})
        self.assertTrue(all(r["durable"] for r in first+second))
        self.assertEqual({r["status"] for r in second},{"ALREADY_PRESENT"})
        self.assertEqual(UT._digest(UT.policy_snapshot()),"613b4f153f7878891c9fcab5014b01be1b10148391978f13ea397d53ec72b29b")
        self.assertEqual(ledger[UT.MA_TEACHING_ID]["portfolios"],list(CTC.PORTFOLIO_ORDER))
        self.assertFalse(ledger[UT.MA_TEACHING_ID]["parameter_validation"]["ml_training_performed"])
        self.assertEqual(ledger[UT.BREAKOUT_TEACHING_ID]['teaching_id'],UT.BREAKOUT_TEACHING_ID)

    def test_ma_trace_freezes_original_daily_snapshot_and_geometry(self):
        raw=rebound_raw(self.clock)
        context=TFD.context(raw,"5m",self.clock)
        self.assertEqual(context["event"]["event_type"],"DAILY_MA_REBOUND",context)
        trace=UT.entry_trace(context,"Aggressive")
        self.assertEqual(trace["teaching_id"],UT.MA_TEACHING_ID)
        context["event"]["stop_price"]=1.
        self.assertNotEqual(trace["timeframe_entry_context"]["event"]["stop_price"],1.)
        self.assertTrue(UT.verify_entry_trace(trace))


class PartialHourlyFeedTests(unittest.TestCase):
    def test_hourly_deficits_are_explicit_without_fabricated_returns(self):
        import veritas_intelligence as VI
        raw=dict(asset="BRENT",price=100.,closes=[100.]*60,vols=[0.]*60,
                 taker_buy=[0.]*60,returns=[0.]*59)
        f=FH.base_features(raw,"1h",VI.horizon_bars)
        self.assertEqual(f["ret_24h"],0.)
        self.assertTrue(f["research_feature_availability"]["ret_24h"])
        self.assertFalse(f["research_feature_availability"]["ret_168h"])
        self.assertEqual(f["ret_168h"],0.)
        self.assertEqual(f["research_hourly_bar_count"],60)
        for count in (0,1,10):
            short=dict(raw,closes=[100.]*count,vols=[0.]*count,taker_buy=[0.]*count,returns=[])
            self.assertEqual(FH.base_features(short,"1h",VI.horizon_bars)["trend"],0.)

    def test_missing_hourly_feed_does_not_crash_native_fast_feature_path(self):
        import veritas_intelligence as VI
        clock=datetime.now(timezone.utc)
        raw=rebound_raw(clock,asset="BRENT")
        raw["structure_intraday_bars"]=raw["structure_bars_by_timeframe"]["5m"]
        for count in (0,60):
            raw.update(closes=[100.]*count,highs=[100.5]*count,lows=[99.5]*count,
                       vols=[0.]*count,taker_buy=[0.]*count,returns=[0.]*max(0,count-1))
            for tf in ("1m","5m","1h","4h","1d","3d","7d"):
                with self.subTest(count=count,timeframe=tf):
                    f=VI.features(dict(raw),tf)
                    self.assertEqual(f["research_hourly_bar_count"],count)
                    self.assertEqual(f["timeframe_entry_context"]["timeframe"],tf)
                    self.assertEqual(f["sma200"],100.)


if __name__=="__main__":
    unittest.main()
