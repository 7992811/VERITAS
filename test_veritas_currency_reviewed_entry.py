"""Immutable reviewed LIVE previews become proposals only, entirely offline.

The native event, canonical sizing/economics and transactional approval store
are real. The explicit authority double accepts only the exact reviewed hash;
no broker, Telegram or production service is contacted.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import veritas_currency_trade_plan as P
import veritas_currency_trading as C
from veritas_trade_approvals import TradeApprovals, TABLE
from test_veritas_currency_structural_trading import native_fixture
from test_veritas_trade_approvals import SQLiteConnection, KEY, OWNER, BOT


class ReviewedEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = str(Path(self.temp.name) / "reviewed-entry.sqlite")
        self.repo = TradeApprovals(lambda: SQLiteConnection(path), KEY, clock=lambda: self.now)
        self.repo.ensure_schema()
        self.reset_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def reset_fixture(self, direction="LONG"):
        self.row, _, spec, account, quote, self.now = native_fixture(direction=direction)
        self.facts = C.TradeFacts(spec, account, quote)
        self.fact_reads, self.authority_calls, self.preview = 0, [], None
        self.after_read = None
        self.coordinator = C.CurrencyTradingCoordinator(repository=self.repo,
            adapter=SimpleNamespace(environment="production",
                submit_limit=lambda *_a, **_k:self.fail("preparation must not submit")),
            account_id=account.account_id, owner=C.TradeOwner(OWNER, OWNER, BOT),
            facts=self.read_facts, summary=lambda:[deepcopy(self.row)],
            ingest_execution=lambda *_:self.fail("preparation must not ingest"),
            clock=lambda:self.now, live_admission=self.blocking_authority,
            preflight_live=True, execution_enabled=False)
        with self.assertRaisesRegex(P.TradePlanBlocked, "LIVE_ACCOUNT_ADMISSION_REQUIRED"):
            self.coordinator.prepare()
        self.assertIsNotNone(self.preview)
        self.coordinator.preflight_live = False
        self.coordinator.live_admission = self.exact_hash_authority
        self.fact_reads, self.authority_calls = 0, []

    def read_facts(self):
        self.fact_reads += 1
        if self.after_read:
            self.after_read(self.fact_reads)
        return self.facts

    def blocking_authority(self, **kwargs):
        self.preview = deepcopy(kwargs["terms"])
        return {"eligible": False, "blockers": ["INDEPENDENT_EVIDENCE_REQUIRED"]}

    def exact_hash_authority(self, **kwargs):
        self.authority_calls.append(deepcopy(kwargs["terms"]))
        self.assertEqual(P.fingerprint(kwargs["terms"]), P.fingerprint(self.preview))
        return {"eligible": True, "blockers": [],
                "valid_until": (self.now+timedelta(seconds=10)).isoformat()}

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.facts = replace(self.facts,
            spec=replace(self.facts.spec, observed_at=self.now),
            account=replace(self.facts.account, observed_at=self.now),
            quote=replace(self.facts.quote, observed_at=self.now))

    def assert_not_persisted(self):
        with SQLiteConnection(str(Path(self.temp.name) / "reviewed-entry.sqlite")) as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {TABLE}").fetchone()
            self.assertEqual(row["n"], 0)

    def test_retry_preserves_every_reviewed_byte_with_new_facts_both_directions(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                self.reset_fixture(direction)
                frozen = deepcopy(self.preview)
                self.advance(2)
                result = self.coordinator.prepare_reviewed_entry(self.preview)
                self.assertEqual(result["terms"], frozen)
                self.assertEqual(result["terms_hash"], P.fingerprint(frozen))
                self.assertEqual(self.preview, frozen)
                self.assertEqual(self.authority_calls, [frozen])
                self.assertEqual(self.fact_reads, 2)
                self.assertEqual(result["status"], "PENDING_DELIVERY")
                self.assertEqual((result["owner_user_id"], result["private_chat_id"], result["bot_id"]),
                                 (OWNER, OWNER, BOT))
                self.assertEqual(P.utc(result["expires_at"]), self.now+timedelta(seconds=120))
                self.assertEqual(self.coordinator.execute_approved(result["proposal_id"])["code"],
                                 "PROPOSAL_NOT_APPROVED")

    def test_live_authority_is_mandatory_even_when_normal_preflight_is_off(self):
        self.coordinator.live_admission = None
        with self.assertRaisesRegex(P.TradePlanBlocked, "LIVE_ACCOUNT_ADMISSION_REQUIRED"):
            self.coordinator.prepare_reviewed_entry(self.preview)
        self.coordinator.live_admission = lambda **_: {"eligible": True, "blockers": ["BLOCKED"]}
        with self.assertRaisesRegex(P.TradePlanBlocked, "LIVE_ACCOUNT_ADMISSION_REQUIRED"):
            self.coordinator.prepare_reviewed_entry(self.preview)
        self.assert_not_persisted()

    def test_add_retains_exact_held_baseline_and_still_needs_owner_approval(self):
        held = {key:deepcopy(self.preview[key]) for key in
                ("horizon", "source_identity", "stop_price", "target_price", "canonical_event_id")}
        self.facts = replace(self.facts, held_terms=held,
            account=replace(self.facts.account, signed_lots=2, managed_signed_lots=2))
        self.coordinator.preflight_live = True
        self.coordinator.live_admission = self.blocking_authority
        with self.assertRaises(P.TradePlanBlocked):
            self.coordinator.prepare()
        self.assertEqual(self.preview["action"], "ADD")
        self.coordinator.preflight_live = False
        self.coordinator.live_admission = self.exact_hash_authority
        self.advance(2)
        result = self.coordinator.prepare_reviewed_entry(self.preview)
        self.assertEqual(result["terms"], self.preview)
        self.assertEqual(result["status"], "PENDING_DELIVERY")
        self.assertEqual(result["terms"]["position_before_lots"], 2)
        self.assertEqual(result["terms"]["stop_price"], held["stop_price"])

    def test_only_production_open_or_add_exact_terms_are_accepted(self):
        cases = [dict(action="CLOSE", reduce_only=True, side="SELL"),
                 dict(execution_environment="sandbox"), dict(account_id="another-account"),
                 dict(instrument_uid="another-uid"), dict(asset="BRENT"),
                 dict(side="SELL"), dict(reduce_only=True), dict(order_type="MARKET"),
                 dict(time_in_force="TIME_IN_FORCE_DAY"), dict(price_type="CURRENCY"),
                 dict(protective_order_mode="AUTOMATIC_STOP"), dict(lots=self.preview["lots"]+1),
                 dict(lots=self.preview["lots"]-1), dict(target_fraction="3"),
                 dict(required_margin_rub="0"), dict(estimated_commission_rub="0"),
                 dict(currency_nav_rub="1000000"), dict(canonical_cost_multiple="1"),
                 dict(expected_hold_seconds="1"), dict(extra_safety_bypass=True)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(P.TradePlanBlocked):
                self.coordinator.prepare_reviewed_entry(dict(deepcopy(self.preview), **changes))
        self.coordinator.adapter.environment = "sandbox"
        with self.assertRaisesRegex(P.TradePlanBlocked, "REVIEWED_PRODUCTION_ENTRY_REQUIRED"):
            self.coordinator.prepare_reviewed_entry(self.preview)
        self.assertEqual(self.authority_calls, [])
        self.assert_not_persisted()

    def test_nested_economic_and_native_fields_cannot_escape_exact_comparison(self):
        for kind in ("economics", "source", "native", "extra_native"):
            altered = deepcopy(self.preview)
            if kind == "economics":
                altered["economics"]["modeled_round_trip_cost_pct"] = "0"
            elif kind == "source":
                altered["source_identity"]["contract_id"] = "another-source"
            else:
                native = P.approved_entry_context(altered)
                if kind == "native":
                    native["event"]["stop_price"] *= .99
                else:
                    native["unapproved_field"] = True
                altered["entry_context"] = P.json_safe(native)
                altered["entry_context_json"] = P.native_context_json(native)
            with self.subTest(kind=kind), self.assertRaises(P.TradePlanBlocked):
                self.coordinator.prepare_reviewed_entry(altered)
        self.assertEqual(self.authority_calls, [])
        self.assert_not_persisted()

    def test_invalid_or_oversized_terms_are_blocked_before_facts_and_authority(self):
        cases = [None, [], "{}", dict(self.preview, enormous="x"*65536),
                 dict(self.preview, lots=1.0), dict(self.preview, limit_price=self.preview["limit_price"]+"0")]
        for terms in cases:
            with self.subTest(kind=type(terms).__name__), self.assertRaises(P.TradePlanBlocked):
                self.coordinator.prepare_reviewed_entry(terms)
        self.assertEqual(self.fact_reads, 0)
        self.assertEqual(self.authority_calls, [])
        self.assert_not_persisted()

    def test_changed_price_requires_new_review_even_if_favorable(self):
        self.advance(2)
        self.facts = replace(self.facts, quote=replace(self.facts.quote,
            bid=self.facts.quote.bid-D(".001"), ask=self.facts.quote.ask-D(".001")))
        with self.assertRaisesRegex(P.TradePlanBlocked, "REVIEWED_CANONICAL_TERMS_CHANGED"):
            self.coordinator.prepare_reviewed_entry(self.preview)
        self.assertEqual(self.authority_calls, [])
        self.assert_not_persisted()

    def test_stale_quote_or_future_review_timestamps_do_not_create_proposal(self):
        quote = self.facts.quote
        self.advance(16)
        self.facts = replace(self.facts, quote=quote)
        with self.assertRaisesRegex(P.TradePlanBlocked, "BROKER_QUOTE_STALE"):
            self.coordinator.prepare_reviewed_entry(self.preview)
        self.advance(1)
        for key in ("prepared_at", "quote_observed_at", "account_observed_at"):
            altered = dict(self.preview, **{key:(self.now+timedelta(seconds=1)).isoformat()})
            with self.subTest(key=key), self.assertRaisesRegex(P.TradePlanBlocked, "FROM_FUTURE"):
                self.coordinator.prepare_reviewed_entry(altered)
        self.assertEqual(self.authority_calls, [])
        self.assert_not_persisted()

    def test_fresh_facts_and_canonical_event_are_rechecked_after_authority_io(self):
        for change in ("ledger", "costs", "event", "quote"):
            with self.subTest(change=change):
                self.reset_fixture()
                def authority(**kwargs):
                    result = self.exact_hash_authority(**kwargs)
                    self.advance(1)
                    if change == "ledger":
                        self.facts = replace(self.facts, account=replace(self.facts.account, ledger_revision=2))
                    elif change == "costs":
                        self.facts = replace(self.facts, account=replace(self.facts.account, costs_reconciled=False))
                    elif change == "event":
                        self.row = {}
                    else:
                        self.facts = replace(self.facts, quote=replace(self.facts.quote, limit_orders_available=False))
                    return result
                self.coordinator.live_admission = authority
                with self.assertRaises(P.TradePlanBlocked):
                    self.coordinator.prepare_reviewed_entry(self.preview)
                self.assertEqual(len(self.authority_calls), 1)
                self.assertEqual(self.fact_reads, 2)
                self.assert_not_persisted()

    def test_evidence_expiry_during_final_facts_read_cannot_extend_authority(self):
        self.after_read = lambda count: self.advance(11) if count == 2 else None
        with self.assertRaisesRegex(P.TradePlanBlocked, "LIVE_ADMISSION_EXPIRED"):
            self.coordinator.prepare_reviewed_entry(self.preview)
        self.assertEqual(len(self.authority_calls), 1)
        self.assert_not_persisted()

    def test_normal_preparation_authority_uses_exact_persisted_decimal_encoding(self):
        self.facts = replace(self.facts, quote=replace(self.facts.quote, ask=D("100.610"), bid=D("100.600")))
        self.coordinator.preflight_live = True
        self.coordinator.live_admission = self.blocking_authority
        with self.assertRaises(P.TradePlanBlocked):
            self.coordinator.prepare()
        self.assertEqual(self.preview["limit_price"], "100.61")
        self.coordinator.live_admission = self.exact_hash_authority
        result = self.coordinator.prepare()
        self.assertEqual(result["terms_hash"], P.fingerprint(self.authority_calls[-1]))
        self.assertEqual(result["terms"], self.preview)
        self.assertTrue(self.coordinator._event_still_valid(result["terms"], self.facts, self.now))


if __name__ == "__main__":
    unittest.main()
