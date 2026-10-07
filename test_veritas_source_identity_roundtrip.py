"""Stored identities survive final planning; actual foreign quotes stay blocked."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_timeframe_policy as TFP
import veritas_timeframe_structure as TS


class IdentityRoundTripTests(unittest.TestCase):
    def tbank_identity(self, uid="cny-contract-a", asset="CNYRUBF"):
        return VPS.identity(asset, {"source": "TBANK_GRPC_" + asset,
                                    "contract": {"instrument_uid": uid}})

    def test_feed_identity_still_requires_nested_broker_uid(self):
        self.assertIsNone(VPS.identity("CNYRUBF", {
            "source": "TBANK_GRPC_CNYRUBF", "contract_id": "cny-contract-a"}))
        self.assertIsNone(VPS.identity("CNYRUBF", {
            "source": "TBANK_GRPC_CNYRUBF", "contract": {"secid": "CNYRUBF"}}))

    def test_canonical_source_and_contract_round_trip(self):
        examples = [
            ("CNYRUBF", {"source": "TBANK_GRPC_CNYRUBF", "contract": {"instrument_uid": "cny-a"}}),
            ("NQ", {"source": "TBANK_GRPC_NQ", "contract": {"instrument_uid": "nq-a"}}),
            ("MOEX", {"source": "TBANK_GRPC_MOEX", "contract": {"instrument_uid": "mx-a"}}),
            ("GOLD", {"source": "ProFinance", "raw_label": "Gold"}),
            ("NQ", {"source": "ProFinance NASD100_FUT", "raw_label": "NASD100_FUT"}),
            ("BRENT", {"source": "ProFinance", "raw_label": "Brent oil"}),
            ("GOLD", {"source": "Yahoo Gold GC=F", "contract_id": "GCZ26"}),
            ("CNYRUBF", {"source": "MOEX ISS CNYRUBF", "contract_id": "CNYRUBF"}),
            ("BTC", {"source": "Binance spot"}),
            ("ETH", {"source": "Coinbase spot"}),
        ]
        for asset, quote in examples:
            with self.subTest(asset=asset, source=quote["source"]):
                expected = VPS.identity(asset, quote)
                snapshot = deepcopy(expected)
                fields = VPS.quote_identity_fields(expected)
                self.assertTrue(VPS.same(expected, VPS.identity(asset, fields)))
                self.assertEqual(expected, snapshot)
                self.assertFalse({"price", "observed_at", "source_gate_pass"} & fields.keys())
        self.assertEqual(VPS.quote_identity_fields(self.tbank_identity())["contract"],
                         {"instrument_uid": "cny-contract-a"})

    def test_incomplete_or_inconsistent_stored_identity_is_not_reconstructed(self):
        correct = self.tbank_identity()
        for identity in (None, {}, "unlabelled", dict(correct, key=""), dict(correct, asset=[]),
                         dict(correct, primary_source={}),
                         dict(correct, contract_id=None), dict(correct, asset="NQ"),
                         dict(correct, key="MOEX:CNYRUBF")):
            with self.subTest(identity=identity):
                self.assertEqual(VPS.quote_identity_fields(identity), {})

    def row(self):
        clock = datetime.now(timezone.utc).replace(microsecond=0)
        step = 300
        begin = clock.timestamp() - 10 - 33 * step
        bars = [dict(ts=begin + i * step, open=100., high=100.5, low=99.5,
                     close=100., timeframe="5m", volume=100.) for i in range(32)]
        bars[24]["high"] = 101.
        bars[27]["low"] = 99.
        bars.append(dict(ts=begin + 32 * step, open=100., high=101.3, low=100.,
                         close=101.2, timeframe="5m", volume=150.))
        identity = self.tbank_identity()
        ctx = TS.build_context(bars, "5m", clock, asset="CNYRUBF",
                               source_identity=identity, config=CTC.STRUCTURAL_ENTRY_POLICY)
        self.assertEqual(ctx["event"]["direction"], "LONG")
        row = dict(asset="CNYRUBF", horizon="5m", research_decision="LONG", price=101.2,
                   source="TBANK_GRPC_CNYRUBF", contract={"instrument_uid": "cny-contract-a"},
                   market_observed_at=clock.isoformat(), timeframe_entry_context=ctx,
                   trade_plan={"horizon": "5m", "market_observed_at": clock.isoformat()})
        return row, clock

    def test_final_plan_keeps_same_broker_contract_identity(self):
        row, clock = self.row()
        prepared = TFP.prepare_row(row, now=clock)
        self.assertTrue(prepared["trade_plan"]["entry_timing_gate"]["eligible"])
        plan = TFP.final_plan("CNYRUBF", "LONG", prepared["trade_plan"], clock)
        self.assertTrue(plan["entry_timing_gate"]["eligible"], plan)
        self.assertNotIn("SAME_TF_SOURCE_MISMATCH", plan["final_economics_gate"]["blockers"])
        self.assertEqual(plan["timeframe_entry_context"]["source_identity"], self.tbank_identity())

    def test_actual_quote_from_other_contract_or_missing_uid_stays_rejected(self):
        for contract in ({"instrument_uid": "cny-contract-b"}, {}, {"secid": "CNYRUBF"}):
            row, clock = self.row()
            row.update(contract=contract, contract_id="cny-contract-a")
            plan = TFP.prepare_row(row, now=clock)["trade_plan"]
            self.assertFalse(plan["eligible"])
            self.assertEqual(plan["entry_timing_gate"]["reason"], "SAME_TF_SOURCE_MISMATCH")

    def test_different_broker_contracts_cannot_share_a_source_lock(self):
        first, other = self.tbank_identity(), self.tbank_identity("cny-contract-b")
        self.assertFalse(VPS.same(first, other))
        self.assertFalse(VPS.same(first, VPS.identity("CNYRUBF", VPS.quote_identity_fields(other))))


if __name__ == "__main__":
    unittest.main()
