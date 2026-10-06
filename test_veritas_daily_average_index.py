"""Exact causal parity and normalization-count tests for indexed daily data."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest import mock

import veritas_daily_averages as DA
from test_veritas_daily_averages import DAY, PF, BTC, fixture


class DailyAverageIndexTests(unittest.TestCase):
    def assert_parity(self, bars, clocks, identity=PF, config=None, periods=(18,50,200)):
        kwargs={"asset":identity.get("asset","NQ"),"source_identity":identity,
                "periods":periods,"config":config}
        indexed=DA.context_builder(bars,**kwargs)
        for clock in clocks:
            with self.subTest(clock=str(clock)):
                self.assertEqual(indexed(clock),DA.build_context(bars,clock,**kwargs))
        return indexed

    def test_every_daily_prefix_matches_reference_exactly(self):
        bars=fixture(260)
        clocks=[d*DAY for d in range(1,263)]+[265*DAY+60,265*DAY+61]
        self.assert_parity(bars,clocks)

    def test_delayed_duplicate_and_changed_first_observation_are_causal(self):
        bars=fixture(240)
        # This row appears first in input order but becomes observable later.
        first=deepcopy(bars[210])
        first.update(close=first["close"]+.2,available_at=241*DAY+7)
        later=deepcopy(bars[210])
        later["available_at"]=242*DAY+11
        # Same source/price but a different available_at is a conflict too.
        bars=[first,*bars,later]
        clocks=[211*DAY,240*DAY,241*DAY+6,241*DAY+7,242*DAY+10,242*DAY+11]
        self.assert_parity(bars,clocks)

    def test_future_bad_source_and_timestamp_boundary_diagnostics_match(self):
        bars=fixture(240)
        bad=deepcopy(bars[-1])
        bad["source_identity"]={"key":"YAHOO:NQ=F"}
        bad["available_at"]=242*DAY
        malformed={"ts":239*DAY,"end_ts":None,"available_at":None}
        bars += [None,{"ts":"bad"},malformed,bad]
        self.assert_parity(bars,[238*DAY,239*DAY,239*DAY+.00001,240*DAY,242*DAY-1,242*DAY])

    def test_out_of_order_clocks_preserve_state_and_expiry(self):
        bars=fixture(240)
        self.assert_parity(bars,[244*DAY+61,200*DAY,239*DAY,180*DAY,240*DAY,
                                0,241*DAY,240*DAY+3600,244*DAY+60])

    def test_crypto_gap_and_native_exchange_short_session_match(self):
        crypto=fixture(240,identity=BTC)
        del crypto[205]
        self.assert_parity(crypto,[200*DAY,210*DAY,240*DAY,241*DAY+61],identity=BTC)
        identity={"key":"MOEX:CNYRUBF","contract_id":"CNYRUBF","asset":"CNYRUBF"}
        short=fixture(240,identity=identity)
        for row in short:
            row["ts"] += 9*3600
            row["native_interval"]=24
            row["provider_end_ts"]=row["end_ts"]-1
        self.assert_parity(short,[220*DAY,220*DAY+3600,240*DAY],identity=identity)

    def test_duplicates_taint_and_maximum_history_truncation_match(self):
        bars=fixture(620)
        bad=deepcopy(bars[500])
        bad["low"]=bad["high"]+1
        bars += [deepcopy(bars[499]),bad,deepcopy(bars[500])]
        self.assert_parity(bars,[480*DAY,501*DAY,550*DAY,620*DAY,610*DAY])

    def test_invalid_clock_identity_and_config_follow_reference(self):
        bars=fixture(240)
        self.assert_parity(bars,[True,"2026-10-07T10:00:00",float("nan"),
                                datetime(2026,10,7),datetime.fromtimestamp(240*DAY,timezone.utc)])
        indexed=DA.context_builder(bars,asset="GOLD",source_identity=PF)
        self.assertEqual(indexed(240*DAY),
                         DA.build_context(bars,240*DAY,asset="GOLD",source_identity=PF))
        self.assert_parity(bars,[240*DAY,242*DAY+61],config={"max_daily_age_days":2,"slope_lookback_days":5})

    def test_input_and_return_mutation_do_not_change_future_snapshots(self):
        bars=fixture(240)
        reference=deepcopy(bars)
        kwargs={"asset":"NQ","source_identity":PF}
        indexed=DA.context_builder(bars,**kwargs)
        first=indexed(239*DAY)
        first["periods"]["50"]["value"]=-1
        first["diagnostics"]["tainted_timestamps"].append(1)
        bars[-1]["close"]=-1
        self.assertEqual(indexed(240*DAY),DA.build_context(reference,240*DAY,**kwargs))

    def test_daily_normalization_happens_once_not_for_each_snapshot(self):
        bars=fixture(500)
        with mock.patch.object(DA,"_native",wraps=DA._native) as native:
            indexed=DA.context_builder(bars,asset="NQ",source_identity=PF)
            for day in range(22,501):
                indexed(day*DAY)
            self.assertEqual(native.call_count,500)
        with mock.patch.object(DA,"_native",wraps=DA._native) as native:
            for day in (200,300,400,500):
                DA.build_context(bars,day*DAY,asset="NQ",source_identity=PF)
            self.assertGreater(native.call_count,1000)

    def test_forming_unfinalized_days_do_not_influence_earlier_snapshots(self):
        bars=fixture(240)
        bars[-1]["finalized"]=False
        self.assert_parity(bars,[239*DAY,240*DAY-1,240*DAY,241*DAY])


if __name__=="__main__":
    unittest.main()
