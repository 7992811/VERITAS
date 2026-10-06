"""Daily MA50/200 evidence through signed Currency approvals, entirely offline.

The source histories come from the native MA rebound regression fixture. Every
daily/local OHLC value is transformed together to a CNY-sized price/volatility.
The positive storage tests explicitly request fraction 3 only in a copied unit
admission so the 10,000 RUB allocation can contain whole futures contracts.
All native MA, structural, economics and final risk gates remain real.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import json
import tempfile
import unittest

import veritas_canonical_constitution as CTC
import veritas_currency_trade_plan as P
import veritas_ma_rebound as MA
import veritas_price_source as VPS
import veritas_tbank_trading as T
import veritas_trade_approvals as A
from test_veritas_currency_notifications import Connection
from test_veritas_ma_rebound import example


ACCOUNT = "ma-test-account"
UID = "11111111-1111-4111-8111-111111111111"
OWNER, BOT = 123456789, 987654321


class CurrencyMAApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ma_approvals.sqlite")
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.repo = A.TradeApprovals(
            lambda: Connection(self.path), b"synthetic-ma-approval-signing-key-32-bytes",
            clock=lambda: self.now)
        self.repo.ensure_schema()

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, period=50, short=False):
        local, daily, stamp = example(period=period, short=short, timeframe="5m")
        self.now = datetime.fromtimestamp(stamp, timezone.utc)
        source = VPS.identity("CNYRUBF", {
            "source": "MOEX ISS CNYRUBF", "contract_id": "CNYRUBF"})
        width = .26
        factor = 12.345 / (100. + width * (local[-1]["close"] - 100.))
        for bar in local + daily:
            for field in ("open", "high", "low", "close"):
                bar[field] = (100. + width * (bar[field] - 100.)) * factor
            bar["source_identity"] = deepcopy(source)
        context = MA.build_context(
            local, "5m", stamp, daily_bars=daily, asset="CNYRUBF",
            source_identity=source, config=CTC.STRUCTURAL_ENTRY_POLICY,
            ma_config={"periods": (period,)})
        event = context.get("event")
        self.assertIsNotNone(event, context)
        self.assertEqual(event["event_type"], "DAILY_MA_REBOUND")
        self.assertEqual(event["ma_proof"]["period"], period)
        self.assertTrue(MA.validate_event(event, source)["eligible"])
        direction = "SHORT" if short else "LONG"
        row = {
            "asset": "CNYRUBF", "horizon": "5m",
            "research_decision": direction, "decision": direction,
            "price": event["signal_price"], "confidence": .90,
            "signal_tier": "SUPER_" + direction,
            "source": "MOEX ISS CNYRUBF", "contract_id": "CNYRUBF",
            "source_names": {"primary": "MOEX ISS CNYRUBF"},
            "source_gate_pass": True, "market_open": True, "direct_sources": 1,
            "market_observed_at": self.now.isoformat(), "paper_eligible": True,
            "horizon_structure": {"direction": direction, "state": "CONFIRMED_TREND", "score": .90},
            "independent_evidence_families": 5,
            "_local_execution_context": {"same_direction_count": 1, "opposite_direction_count": 0},
            "timeframe_entry_context": context,
            "trade_plan": {"horizon": "5m", "market_observed_at": self.now.isoformat(),
                           "entry_price": event["signal_price"],
                           "stop_price": event["stop_price"], "target_price": event["target_price"]},
        }
        admission = P.VCR.evaluate(row, CTC.runtime_portfolio_policy("Currency"), 0., self.now)
        self.assertTrue(admission["open"], admission)
        admission = deepcopy(admission)
        admission["fraction"] = 3  # Isolated storage test; never a production signal.
        spec = P.ContractSpec(UID, "CNYRUBF", 1, D(".001"), D("1"),
                              D("1000"), D("1000"), True, True, True, self.now)
        account = P.AccountSnapshot(ACCOUNT, T.FULL_ACCESS, D("10000"), D("10000"),
                                    D("10000"), 0, 0, 0, 0, 100, 100, 1, self.now, True)
        quote = P.BrokerQuote(UID, D("12.345") if short else D("12.344"),
                             D("12.346") if short else D("12.345"), self.now, True)
        terms = P.prepare_entry(row, admission, spec, account, quote, now=self.now)
        return terms, context, spec, account, quote

    def persist(self, terms):
        proposal = self.repo.create(
            terms, owner_user_id=OWNER, private_chat_id=OWNER, bot_id=BOT,
            expires_at=self.now + timedelta(seconds=120))
        # Read through a newly constructed repository to require the stored JSON
        # and signature, rather than a Python object retained by create().
        reader = A.TradeApprovals(
            lambda: Connection(self.path), b"synthetic-ma-approval-signing-key-32-bytes",
            clock=lambda: self.now)
        return reader.get(proposal["proposal_id"])

    def assert_no_float_values(self, value):
        if isinstance(value, dict):
            for item in value.values():
                self.assert_no_float_values(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_float_values(item)
        else:
            self.assertNotIsInstance(value, float)

    def test_native_ma50_ma200_both_directions_survive_signed_storage_revalidation(self):
        for period in (50, 200):
            for short in (False, True):
                with self.subTest(period=period, short=short):
                    terms, context, spec, account, quote = self.fixture(period, short)
                    stored = self.persist(terms)["terms"]
                    self.assert_no_float_values(stored)
                    self.assertEqual(stored["lots"], 2)
                    for field in ("limit_price", "stop_price", "target_price",
                                  "order_notional_rub", "required_margin_rub"):
                        self.assertIsInstance(stored[field], str)
                        self.assertTrue(D(stored[field]).is_finite())
                    self.assertEqual(stored["entry_context_json"], P.native_context_json(context))
                    native = P.approved_entry_context(stored)
                    self.assertEqual(native, json.loads(P.native_context_json(context)))
                    self.assertIsInstance(native["event"]["ma_proof"]["ma_value"], float)
                    self.assertIsInstance(native["event"]["signal_at"], float)
                    self.assertIsInstance(stored["entry_context"]["event"]["signal_at"], str)
                    self.assertTrue(MA.validate_event(native["event"], native["source_identity"])["eligible"])
                    self.assertTrue(P.TS.entry_gate(native, float(quote.bid if short else quote.ask),
                                                   stored["direction"], self.now,
                                                   config=CTC.STRUCTURAL_ENTRY_POLICY)["eligible"])
                    P.revalidate(stored, spec, account, quote, now=self.now, canonical_event_valid=True)

    def test_native_or_normalized_proof_tampering_fails_representation_check(self):
        terms, context, spec, account, quote = self.fixture()
        original = self.persist(terms)["terms"]
        for changed_representation in ("display", "native"):
            changed = deepcopy(original)
            native = json.loads(changed["entry_context_json"])
            native["event"]["ma_proof"]["ma_value"] *= .9
            if changed_representation == "native":
                changed["entry_context_json"] = P.native_context_json(native)
            else:
                changed["entry_context"] = P.json_safe(native)
            with self.subTest(representation=changed_representation), self.assertRaisesRegex(
                    P.TradePlanBlocked, "ENTRY_CONTEXT_REPRESENTATION_MISMATCH"):
                P.revalidate(changed, spec, account, quote, now=self.now, canonical_event_valid=True)
        self.assertEqual(P.approved_entry_context(original), json.loads(P.native_context_json(context)))

    def test_consistently_reencoded_invalid_ma_proof_still_fails_native_gate(self):
        terms, context, spec, account, quote = self.fixture()
        stored = self.persist(terms)["terms"]
        invalid = P.approved_entry_context(stored)
        invalid["event"]["ma_proof"]["ma_value"] *= .9
        stored["entry_context"] = P.json_safe(invalid)
        stored["entry_context_json"] = P.native_context_json(invalid)
        # Matching representations do not replace the daily/local causal proof.
        self.assertEqual(P.approved_entry_context(stored), invalid)
        with self.assertRaisesRegex(P.TradePlanBlocked, "MA_REBOUND_PROVENANCE_INVALID"):
            P.revalidate(stored, spec, account, quote, now=self.now, canonical_event_valid=True)

    def test_native_context_text_is_covered_by_durable_terms_signature(self):
        terms, context, spec, account, quote = self.fixture()
        proposal = self.persist(terms)
        changed = deepcopy(proposal["terms"])
        native = json.loads(changed["entry_context_json"])
        native["event"]["ma_proof"]["ma_value"] *= .9
        changed["entry_context_json"] = P.native_context_json(native)
        with Connection(self.path) as connection:
            connection.execute(
                f"UPDATE {A.TABLE} SET terms_json=%s::jsonb WHERE proposal_id=%s",
                (json.dumps(changed, sort_keys=True, allow_nan=False), proposal["proposal_id"]))
        with self.assertRaisesRegex(A.ApprovalError, "TERMS_INTEGRITY_FAILED"):
            self.repo.get(proposal["proposal_id"])


if __name__ == "__main__":
    unittest.main()
