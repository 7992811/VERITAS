"""Current quote-breakout proof through Currency broker proposals, entirely offline.

All histories, account balances and contract tick values are synthetic. The
small synthetic ruble tick value makes whole lots fit the unchanged 2% risk
budget. Native proof, canonical admission and LIVE economics are never mocked.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_currency_trade_plan as P
import veritas_currency_trading as C
import veritas_price_source as VPS
import veritas_structural_breakout as SB
from veritas_trade_approvals import TradeApprovals
from test_veritas_structural_breakout import raw_at, bars, CLOSES, POLICY
from test_veritas_trade_approvals import SQLiteConnection, KEY, OWNER, BOT


UID = "c300543d-aa18-4249-b110-615409dde036"
CLOCK = datetime(2026, 10, 7, 8, 0, 0, 100000, tzinfo=timezone.utc)


def native_fixture(*, direction="LONG", low_rr=False, source_uid=UID):
    # Raise nearby lows before rebuilding every structural proof. This gives
    # a causal nearby support and enough single-target LIVE reward/risk.
    closes = list(CLOSES) if low_rr else [99 if x in (96, 98) else x for x in CLOSES]
    start = CLOCK - timedelta(hours=len(closes), milliseconds=100)
    raw, now = raw_at(price=103.8 if low_rr else 100.6, at=CLOCK,
                      hourly=bars(closes, "1h", start))
    raw["contract"]["instrument_uid"] = source_uid
    source = VPS.identity("CNYRUBF", raw)
    raw["structure_source_identity"] = source
    for lane in raw["structure_bars_by_timeframe"].values():
        for bar in lane:
            if direction == "SHORT":
                original = dict(bar)
                bar.update(open=220-original["open"], close=220-original["close"],
                           high=220-original["low"], low=220-original["high"])
            bar["source_identity"] = deepcopy(source)
    if direction == "SHORT":
        raw.update(price=220-raw["price"], best_bid=220-raw["best_ask"],
                   best_ask=220-raw["best_bid"])
    context = SB.build_context(raw, "1h", now, config=POLICY)
    event = context["event"]
    geometry = SB.entry_gate(context, raw["price"], direction, now)
    if not geometry["eligible"]:
        raise AssertionError(geometry)
    plan = dict(direction=direction, horizon="1h", entry_price=raw["price"],
        stop_price=event["stop_price"], target_price=event["target_price"],
        runner_target_price=event["runner_target_price"], target_ladder=deepcopy(event["target_ladder"]),
        timeframe_entry_context=context, structural_policy_version=CTC.BREAKOUT_LIFECYCLE_POLICY["version"],
        expected_move_pct=geometry["remaining_move_pct"], expected_to_stop_ratio=geometry["reward_risk"],
        expected_hold_seconds=3600., initial_position_fraction=.1, best_bid=raw["best_bid"],
        best_ask=raw["best_ask"], market_observed_at=raw["observed_at"])
    row = dict(raw, horizon="1h", research_decision=direction, decision=direction,
        confidence=.90, signal_tier="SUPER_"+direction, paper_eligible=True, direct_sources=1,
        timeframe_entry_context=context, trade_plan=plan, market_observed_at=raw["observed_at"])
    admission = P.VCR.evaluate(row, CTC.runtime_portfolio_policy("Currency"), 0., now)
    if admission["open"] is not True:
        raise AssertionError(admission)
    broker_time = now + timedelta(seconds=1)
    # These are deliberately synthetic contract valuations, never broker data.
    spec = P.ContractSpec(UID, "CNYRUBF", 1, D(".001"), D(".001"), D("1"), D("1"),
                          True, True, True, broker_time)
    account = P.AccountSnapshot("synthetic-current-sb-account", "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
        D("10000"), D("10000"), D("10000"), 0, 0, 0, 0, 1000, 1000, 1, broker_time, True)
    quote = P.BrokerQuote(UID, D(str(raw["best_bid"])), D(str(raw["best_ask"])), broker_time, True)
    return row, admission, spec, account, quote, broker_time


class CurrentStructuralBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = CLOCK + timedelta(seconds=1)
        path = str(Path(self.temp.name) / "current-structural.sqlite")
        self.repo = TradeApprovals(lambda: SQLiteConnection(path), KEY, clock=lambda: self.now)
        self.repo.ensure_schema()

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, fixture):
        row, admission, spec, account, quote, now = fixture
        return P.prepare_entry(row, admission, spec, account, quote, now=now)

    def test_fresh_broker_book_precedes_canonical_ranking_of_old_research_quote(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, _, spec, account, quote, now = native_fixture(direction=direction)
                later = now + timedelta(seconds=180)
                with self.assertRaisesRegex(P.TradePlanBlocked, "EXECUTION_QUOTE_STALE"):
                    P.select_entry([row], account, later)
                row["_runtime_quote_refresh"] = True
                frozen = deepcopy(row)
                spec, account, quote = (replace(x, observed_at=later) for x in (spec, account, quote))
                # This broker decision uses its own already observed book, not
                # a looser market cache or a second source/network request.
                with patch.object(P.VCR.VPG, "refresh_execution_row", side_effect=AssertionError("unexpected cache refresh")):
                    selected, admission = P.select_entry([row], account, later, spec=spec, quote=quote)
                self.assertTrue(admission["open"])
                price = quote.ask if direction == "LONG" else quote.bid
                self.assertEqual(selected["price"], float(price))
                self.assertEqual(selected["market_observed_at"], later.isoformat())
                self.assertEqual(selected["_execution_quote"]["best_bid"], float(quote.bid))
                self.assertEqual(selected["_execution_quote"]["best_ask"], float(quote.ask))
                terms = P.prepare_entry(selected, admission, spec, account, quote, now=later)
                self.assertEqual(P.approved_entry_context(terms)["event"], frozen["timeframe_entry_context"]["event"])
                self.assertEqual(row, frozen)
                self.assertTrue(all(item["checked_at"] == later.isoformat()
                                    for item in selected["_currency_route_trace"]))

    def test_refresh_does_not_extend_signal_expiry_or_restore_broken_level(self):
        row, _, spec, account, quote, now = native_fixture()
        event = row["timeframe_entry_context"]["event"]
        window = max(CTC.BREAKOUT_LIFECYCLE_POLICY["minimum_entry_window_seconds"],
                     CTC.BREAKOUT_LIFECYCLE_POLICY["max_signal_age_bars"] * P.TS.timeframe_seconds("1h"))
        expired = datetime.fromtimestamp(event["signal_at"] + window + 1, timezone.utc)
        with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_EVENT_EXPIRED"):
            P.select_entry([row], replace(account, observed_at=expired), expired,
                           spec=replace(spec, observed_at=expired), quote=replace(quote, observed_at=expired))
        later = now + timedelta(seconds=180)
        trigger = D(str(event["trigger_level"]))
        with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD"):
            P.select_entry([row], replace(account, observed_at=later), later,
                spec=replace(spec, observed_at=later),
                quote=replace(quote, observed_at=later, bid=trigger-D(".001"), ask=trigger))

    def test_stale_future_or_wrong_contract_broker_book_cannot_refresh_a_candidate(self):
        row, _, spec, account, quote, now = native_fixture()
        for changed, code in ((replace(quote, observed_at=now-timedelta(seconds=16)), "BROKER_QUOTE_STALE"),
                              (replace(quote, observed_at=now+timedelta(seconds=3)), "BROKER_QUOTE_STALE"),
                              (replace(quote, instrument_uid="different-contract"), "QUOTE_INSTRUMENT_MISMATCH"),
                              (replace(quote, limit_orders_available=False), "LIMIT_ORDER_UNAVAILABLE")):
            with self.subTest(code=code), self.assertRaisesRegex(P.TradePlanBlocked, code):
                P.select_entry([row], account, now, spec=spec, quote=changed)
        with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_QUOTE_OUT_OF_ORDER"):
            P.select_entry([row], account, now, spec=spec, quote=replace(quote, observed_at=CLOCK))

    def test_foreign_source_stays_unmodified_and_cannot_hide_current_same_contract(self):
        foreign, _, _, _, _, _ = native_fixture(source_uid="another-contract")
        row, _, spec, account, quote, now = native_fixture()
        later = now + timedelta(seconds=180)
        spec, account, quote = (replace(x, observed_at=later) for x in (spec, account, quote))
        frozen = deepcopy(foreign)
        with self.assertRaisesRegex(P.TradePlanBlocked, "EXECUTION_QUOTE_STALE"):
            P.select_entry([foreign], account, later, spec=spec, quote=quote)
        selected, admitted = P.select_entry([foreign, row], account, later, spec=spec, quote=quote)
        self.assertTrue(admitted["open"])
        self.assertEqual(selected["_execution_quote"]["contract"]["instrument_uid"], UID)
        self.assertEqual(foreign, frozen)

    def test_coordinator_old_summary_reaches_model_preflight_without_creating_order(self):
        row, _, spec, account, quote, now = native_fixture()
        later = now + timedelta(seconds=180)
        spec, account, quote = (replace(x, observed_at=later) for x in (spec, account, quote))
        calls = []
        def deny_model(**kwargs):
            calls.append(kwargs["terms"])
            return {"eligible": False, "blockers": ["LIVE_MODEL_ADMISSION_EVIDENCE_REQUIRED"]}
        coordinator = C.CurrencyTradingCoordinator(repository=self.repo,
            adapter=SimpleNamespace(environment="production"), account_id=account.account_id,
            owner=C.TradeOwner(OWNER, OWNER, BOT), facts=lambda:C.TradeFacts(spec, account, quote),
            summary=lambda:[row], ingest_execution=lambda *_:self.fail("unexpected execution"),
            clock=lambda:later, execution_enabled=False, preflight_live=True, live_admission=deny_model)
        with self.assertRaisesRegex(P.TradePlanBlocked, "LIVE_ACCOUNT_ADMISSION_REQUIRED"):
            coordinator.prepare()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["quote_observed_at"], later.isoformat())
        self.assertEqual(self.repo.list_pending(account_id=account.account_id, owner_user_id=OWNER), [])

    def test_current_native_proof_both_directions_survives_signed_roundtrip_and_fresh_broker_quote(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                fixture = native_fixture(direction=direction)
                row, admission, spec, account, quote, now = fixture
                original_context = deepcopy(row["timeframe_entry_context"])
                event = original_context["event"]
                self.assertEqual(event["version"], SB.VERSION)
                self.assertEqual(event["confirmation"], "VERIFIED_QUOTE_CROSS")
                terms = self.prepare(fixture)
                self.assertGreater(terms["lots"], 0)
                self.assertEqual(terms["economics_mode"], "LIVE")
                self.assertEqual(terms["target_execution_policy"], "SINGLE_TARGET_SEPARATE_CONFIRMATION")
                self.assertEqual(terms["source_identity"]["contract_id"], UID)
                self.assertLessEqual(D(terms["total_stop_risk_rub"]), D("200"))
                self.assertEqual(terms["economics"]["economics_policy"]["execution_mode"], "LIVE")
                self.assertGreater(D(terms["economics"]["minimum_reward_risk"]), 0)
                self.assertEqual(terms["economics"]["target_ladder"], [])
                proposal = self.repo.create(terms, owner_user_id=OWNER, private_chat_id=OWNER, bot_id=BOT)
                stored = self.repo.get(proposal["proposal_id"])["terms"]
                native = P.approved_entry_context(stored)
                self.assertEqual(native["event"], json.loads(P.native_context_json(original_context))["event"])
                self.assertTrue(SB.validate_event(native["event"], native["source_identity"])["eligible"])
                self.assertEqual(native["event"]["target_ladder"], event["target_ladder"])
                self.assertEqual(native["event"]["signal_at"], event["signal_at"])
                self.assertEqual(native["event"]["proof_hash"], event["proof_hash"])
                self.assertEqual(native["quote_observed_at"], quote.observed_at.timestamp())
                self.assertEqual(native["quote"]["price"], float(quote.ask if direction == "LONG" else quote.bid))
                later = now + timedelta(seconds=2)
                refreshed_quote = replace(quote, observed_at=later)
                P.revalidate(stored, replace(spec, observed_at=later), replace(account, observed_at=later),
                             refreshed_quote, now=later, canonical_event_valid=True)
                self.assertEqual(row["timeframe_entry_context"], original_context)
                self.assertEqual(self.repo.get(proposal["proposal_id"])["terms"], stored)

    def test_paper_weighted_low_rr_cannot_admit_live_single_target_order(self):
        fixture = native_fixture(low_rr=True)
        row, _, _, _, _, now = fixture
        paper = P.VX.economics_gate("CNYRUBF", row["trade_plan"], execution_mode="PAPER", now=CLOCK)
        self.assertTrue(paper["eligible"], paper["blockers"])
        self.assertEqual(paper["minimum_reward_risk"], 0)
        self.assertEqual(len(paper["target_ladder"]), 2)
        with self.assertRaisesRegex(P.TradePlanBlocked, "BROKER_PRICE_ECONOMICS:.*RR"):
            self.prepare(fixture)

    def test_source_uid_and_native_horizon_cannot_be_relabelled_at_broker_boundary(self):
        with self.assertRaisesRegex(P.TradePlanBlocked, "SOURCE_MISMATCH"):
            self.prepare(native_fixture(source_uid="different-synthetic-instrument"))
        fixture = list(native_fixture())
        fixture[0] = dict(fixture[0], horizon="5m")
        with self.assertRaisesRegex(P.TradePlanBlocked, "BROKER_SIGNAL_TIMEFRAME_MISMATCH"):
            self.prepare(fixture)

    def test_equal_timestamp_cannot_turn_last_quote_into_ask_or_refresh_signal_time(self):
        row, admission, spec, account, quote, now = native_fixture()
        quote = replace(quote, observed_at=CLOCK)
        with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_QUOTE_OUT_OF_ORDER"):
            P.prepare_entry(row, admission, spec, account, quote, now=now)

    def test_reencoded_corrupted_native_proof_cannot_pass_fresh_quote_revalidation(self):
        fixture = native_fixture()
        _, _, spec, account, quote, now = fixture
        terms = self.prepare(fixture)
        native = P.approved_entry_context(terms)
        native["event"]["stop_price"] *= .9
        terms["entry_context"] = P.json_safe(native)
        terms["entry_context_json"] = P.native_context_json(native)
        with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_EVENT_PROOF_INVALID"):
            P.revalidate(terms, spec, account, quote, now=now, canonical_event_valid=True)

    def test_fresh_broker_quote_cannot_extend_native_event_expiry(self):
        fixture = native_fixture()
        _, _, spec, account, quote, now = fixture
        terms = self.prepare(fixture)
        event = P.approved_entry_context(terms)["event"]
        policy = CTC.BREAKOUT_LIFECYCLE_POLICY
        window = max(policy["minimum_entry_window_seconds"],
                     policy["max_signal_age_bars"] * P.TS.timeframe_seconds(terms["horizon"]))
        later = datetime.fromtimestamp(event["signal_at"] + window + 1, timezone.utc)
        with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_EVENT_EXPIRED"):
            P.revalidate(terms, replace(spec, observed_at=later), replace(account, observed_at=later),
                         replace(quote, observed_at=later), now=later, canonical_event_valid=True)
        self.assertEqual(P.approved_entry_context(terms)["event"], event)

    def test_fresh_account_cannot_hide_stale_quote_or_missing_live_economics_approval(self):
        fixture = native_fixture()
        _, _, spec, account, quote, now = fixture
        terms = self.prepare(fixture)
        later = now + timedelta(seconds=16)
        with self.assertRaisesRegex(P.TradePlanBlocked, "BROKER_QUOTE_STALE"):
            P.revalidate(terms, replace(spec, observed_at=later), replace(account, observed_at=later),
                         quote, now=later, canonical_event_valid=True)
        for field in ("economics_mode", "target_execution_policy"):
            missing = {key: value for key, value in terms.items() if key != field}
            with self.subTest(field=field), self.assertRaisesRegex(
                    P.TradePlanBlocked, "EXPLICIT_LIVE_ECONOMICS_APPROVAL_REQUIRED"):
                P.revalidate(missing, spec, account, quote, now=now, canonical_event_valid=True)

    def test_favorable_broker_price_cannot_hide_lost_breakout_at_approved_limit(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                fixture = native_fixture(direction=direction)
                _, _, spec, account, quote, now = fixture
                terms = self.prepare(fixture)
                event = P.approved_entry_context(terms)["event"]
                trigger = D(str(event["trigger_level"]))
                later = now + timedelta(seconds=2)
                # The limit remains acceptable to the broker, but the fresh
                # executable price no longer holds the original breakout.
                current = replace(quote, observed_at=later,
                    bid=trigger-D(".001") if direction == "LONG" else trigger,
                    ask=trigger if direction == "LONG" else trigger+D(".001"))
                with self.assertRaisesRegex(P.TradePlanBlocked, "STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD"):
                    P.revalidate(terms, replace(spec, observed_at=later), replace(account, observed_at=later),
                                 current, now=later, canonical_event_valid=True)

    def test_current_native_opposite_exit_uses_held_source_and_ignores_entry_economics(self):
        row, _, spec, account, quote, now = native_fixture(direction="SHORT", low_rr=True)
        account = replace(account, signed_lots=2, managed_signed_lots=2, available_margin_rub=D("0"),
                          broker_max_sell_lots=0, costs_reconciled=False)
        held = {"canonical_event_id": "synthetic-held-long", "horizon": "1h",
                "source_identity": P.json_safe(row["timeframe_entry_context"]["source_identity"]),
                "stop_price": "100", "target_price": "130"}
        coordinator = C.CurrencyTradingCoordinator(repository=self.repo,
            adapter=SimpleNamespace(environment="production"), account_id=account.account_id,
            owner=C.TradeOwner(OWNER, OWNER, BOT), facts=lambda:C.TradeFacts(spec, account, quote, held),
            summary=lambda:[row], ingest_execution=lambda *_:self.fail("preparation cannot ingest execution"),
            clock=lambda:now, execution_enabled=False)
        proposal = coordinator.prepare_next()
        terms = proposal["terms"]
        self.assertEqual(terms["action"], "CLOSE")
        self.assertEqual(terms["exit_reason"], "CONFIRMED_OPPOSITE_CANONICAL_EVENT")
        self.assertEqual(terms["exit_trigger_event_id"], row["timeframe_entry_context"]["event"]["event_id"])
        self.assertEqual(terms["source_identity"], held["source_identity"])
        self.assertEqual(terms["horizon"], held["horizon"])
        self.assertTrue(terms["reduce_only"])
        self.assertEqual(proposal["status"], "PENDING_DELIVERY")
        P.revalidate(terms, spec, account, quote, now=now, canonical_event_valid=False)


if __name__ == "__main__":
    unittest.main()
