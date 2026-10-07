"""Fast-lane freshness, provenance, isolation and callback contract tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import threading
import unittest
from unittest.mock import patch

import veritas_breakout_runtime as BR
import veritas_price_source as VPS


NOW = datetime(2026, 10, 7, 4, 1, tzinfo=timezone.utc)
UID = "c300543d-aa18-4249-b110-615409dde036"


def quote(at=NOW, price=12.736, uid=UID):
    return {"asset": "CNYRUBF", "price": price, "observed_at": at.isoformat(),
            "best_bid": price, "best_ask": price+.001,
            "market_open": True, "source_gate_pass": True, "paper_eligible": True,
            "source_names": {"primary": "TBANK_GRPC CNYRUBF"},
            "contract": {"instrument_uid": uid, "normalization_factor": 1.0}}


def market():
    q = quote()
    source = VPS.identity("CNYRUBF", q)
    bars = [{"ts": NOW.timestamp()-3600*(25-i), "open": 12.70, "high": 12.73,
             "low": 12.69, "close": 12.72, "volume": 100, "timeframe": "1h",
             "source_identity": source} for i in range(24)]
    return dict(q, structure_source_identity=source, structure_bars_by_timeframe={"1h": bars})


def signal_row(*, observed=NOW, checked=NOW, eligible=True, event_id="race-event", uid=UID):
    context = {"status": "OK", "timeframe": "1h",
               "event": {"event_id": event_id, "direction": "LONG"}}
    return dict(quote(observed, uid=uid), horizon="1h", research_decision="LONG",
                market_observed_at=observed.isoformat(), snapshot_stale=False,
                trade_entry_checked_at=checked.isoformat(), timeframe_entry_context=context,
                trade_plan={"eligible": eligible, "entry_event_id": event_id,
                            "timeframe_entry_context": context})


def filled_report(row, checked):
    return {"status": "OK", "checked_at": checked.isoformat(),
            "portfolios": [{"name": "Currency", "admission_trace": [{
                "asset": row["asset"], "horizon": row["horizon"],
                "event_id": row["trade_plan"]["entry_event_id"],
                "execution": {"status": "EXECUTED", "checked_at": checked.isoformat(),
                              "order_id": "actual-order", "fill_price": 12.736}}]}]}


class FastQuoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        with BR._cache_lock:
            self.previous_markets, self.previous_rows = BR._markets.copy(), BR._latest_rows.copy()
            BR._markets.clear()
            BR._latest_rows.clear()
        self.ns = {"lock": threading.RLock(), "last_cycle": {"summary": []}}
        self.built, self.executed = [], []

        def build(raw, horizon, now):
            self.built.append((deepcopy(raw), horizon, now))
            return {"status": "OK", "timeframe": horizon, "event": None}

        def execute(rows, now):
            self.executed.append((deepcopy(rows), now))
            return {"status": "OK", "portfolios": [{"name": "Currency", "admission_trace": [
                {"asset": "CNYRUBF", "horizon": "1h", "event_id": "test-event",
                 "execution": {"status": "BLOCKED", "reason": "TEST_CALLBACK"}}]}]}

        self.runtime = BR.BreakoutRuntime(self.ns, execute, context_builder=build)
        self.publish_patch = patch.object(BR.VPG, "publish_quote")
        self.publish_patch.start()
        self.assertTrue(BR.publish_market(market()))

    def tearDown(self):
        self.runtime.close()
        self.publish_patch.stop()
        with BR._cache_lock:
            BR._markets.clear()
            BR._markets.update(self.previous_markets)
            BR._latest_rows.clear()
            BR._latest_rows.update(self.previous_rows)

    def test_runtime_never_deepcopies_the_complete_market_cache(self):
        real_deepcopy = deepcopy

        def bounded_copy(value, memo=None):
            self.assertIsNot(value, BR._markets)
            return real_deepcopy(value) if memo is None else real_deepcopy(value, memo)

        with patch.object(BR, "deepcopy", side_effect=bounded_copy):
            self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.assertEqual(len(self.built), 1)

    def test_missing_or_stale_quote_does_not_copy_native_history(self):
        class HistoryMustStayInCache:
            def __deepcopy__(self, memo):
                raise AssertionError("history was copied without a fresh verified quote")
        BR._markets['CNYRUBF']['structure_bars_by_timeframe']['1h'][0]['evidence'] = HistoryMustStayInCache()
        for quotes in ({}, {'CNYRUBF': quote(NOW-timedelta(hours=1))}):
            with self.subTest(quotes=quotes):
                result = self.runtime.run_once(NOW, quotes=quotes)
                self.assertEqual(result['status'], 'OK')
        self.assertEqual(self.built, [])
        self.assertEqual(self.executed, [])

    def test_source_rollover_between_quote_collection_and_copy_cannot_build_an_event(self):
        def rollover(descriptors, supplied):
            self.assertNotIn('structure_bars_by_timeframe', descriptors['CNYRUBF'])
            replacement = market()
            replacement.update(quote(uid='new-contract'))
            identity = VPS.identity('CNYRUBF', replacement)
            replacement['structure_source_identity'] = identity
            for bar in replacement['structure_bars_by_timeframe']['1h']:
                bar['source_identity'] = identity
            self.assertTrue(BR.publish_market(replacement))
            return {'CNYRUBF': quote()}
        with patch.object(self.runtime, '_quotes', side_effect=rollover):
            result = self.runtime.run_once(NOW)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(self.built, [])
        self.assertEqual(self.executed, [])
        self.assertEqual(BR._markets['CNYRUBF']['structure_source_identity']['contract_id'], 'new-contract')

    def test_per_asset_history_snapshot_is_isolated_from_the_canonical_cache(self):
        before = deepcopy(BR._markets['CNYRUBF'])
        def build(raw, horizon, now):
            self.assertEqual(raw['structure_bars_by_timeframe'], before['structure_bars_by_timeframe'])
            raw['structure_bars_by_timeframe'][horizon][-1]['close'] = -999
            return {'status': 'OK', 'timeframe': horizon, 'event': None}
        with patch.object(self.runtime, 'context_builder', side_effect=build) as builder:
            result = self.runtime.run_once(NOW, quotes={'CNYRUBF': quote()})
        self.assertEqual(result['status'], 'OK')
        builder.assert_called_once()
        self.assertEqual(BR._markets['CNYRUBF'], before)

    def test_duplicate_quote_does_not_recompute_or_reexecute(self):
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.runtime.run_once(NOW+timedelta(seconds=5), quotes={"CNYRUBF": quote()})
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.runtime.state["duplicate_quotes"], 1)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.built[0][0]["observed_at"], NOW.isoformat())

    def test_busy_book_retries_same_fresh_quote_without_redating_real_event(self):
        import veritas_timeframe_data as TFD
        from test_veritas_structural_cny_episodes import episode_raw
        with TFD._STRUCTURAL_LOCK:
            previous = deepcopy(TFD._STRUCTURAL_STATE)
            TFD._STRUCTURAL_STATE.clear()
        with BR._cache_lock:
            BR._markets.clear()
        attempts = []
        def execute(rows, now):
            attempts.append({row['horizon']:(row['timeframe_entry_context']['event']['event_id'],
                                            row['timeframe_entry_context']['event']['signal_at'])
                             for row in rows})
            return {'status':'BUSY' if len(attempts)==1 else 'OK','paper_only':True}
        try:
            self.runtime.context_builder = TFD.structural_context
            first = episode_raw(0,'2026-10-07T04:00:00Z',12.722)
            BR.publish_market(first)
            self.runtime.run_once(NOW-timedelta(minutes=1),quotes={'CNYRUBF':first})
            self.runtime.entry_pass = execute
            crossed = episode_raw(0,NOW.isoformat(),12.736)
            BR.publish_market(crossed)
            first_try = self.runtime.run_once(NOW,quotes={'CNYRUBF':crossed})
            second_try = self.runtime.run_once(NOW+timedelta(seconds=5),quotes={'CNYRUBF':crossed})
            self.assertEqual(first_try['execution']['status'],'BUSY')
            self.assertEqual(second_try['execution']['status'],'OK')
            self.assertEqual(len(attempts),2)
            self.assertIn('1m',attempts[0])
            self.assertEqual(attempts[0],attempts[1])
            self.assertEqual(attempts[0]['1m'][1],NOW.timestamp())
            self.runtime.run_once(NOW+timedelta(seconds=10),quotes={'CNYRUBF':crossed})
            self.assertEqual(len(attempts),2)
        finally:
            with TFD._STRUCTURAL_LOCK:
                TFD._STRUCTURAL_STATE.clear()
                TFD._STRUCTURAL_STATE.update(previous)

    def test_new_provider_observation_can_retry_same_event_without_redating_old_quote(self):
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        newer = NOW+timedelta(seconds=5)
        self.runtime.run_once(newer, quotes={"CNYRUBF": quote(newer, 12.737)})
        self.assertEqual(len(self.built), 2)
        self.assertEqual(self.built[-1][0]["observed_at"], newer.isoformat())

    def test_another_contract_is_rejected_before_context_or_callback(self):
        out = self.runtime.run_once(NOW, quotes={"CNYRUBF": quote(uid="another-contract")})
        self.assertEqual(out["source_rejections"], 1)
        self.assertEqual(self.built, [])
        self.assertEqual(self.executed, [])

    def test_stale_and_out_of_order_quotes_are_not_new_crossings(self):
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote(NOW-timedelta(hours=1))})
        self.assertEqual(self.built, [])
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.runtime.run_once(NOW+timedelta(seconds=5), quotes={"CNYRUBF": quote(NOW-timedelta(seconds=1))})
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.runtime.state["stale_quotes"], 2)

    def test_capture_cannot_replace_newer_history_or_accept_foreign_bars(self):
        older = market()
        older["structure_bars_by_timeframe"]["1h"] = older["structure_bars_by_timeframe"]["1h"][:-3]
        self.assertTrue(BR.publish_market(older))
        self.assertEqual(len(BR._markets["CNYRUBF"]["structure_bars_by_timeframe"]["1h"]), 24)
        foreign = market()
        foreign["structure_bars_by_timeframe"]["1h"][0]["source_identity"] = {"key": "MOEX:CNYRUBF"}
        self.assertFalse(BR.publish_market(foreign))

    def test_broker_history_refresh_is_an_existing_memory_read(self):
        at = NOW-timedelta(hours=1)
        item = {"time": at.isoformat(), "open": 12.73, "high": 12.74,
                "low": 12.72, "close": 12.735, "volume_lots": 200}
        connection = SimpleNamespace(lock=threading.RLock(), candles={
            ("CNYRUBF", "1h"): {"loaded_at": NOW.isoformat(), "status": "OK",
                                "instrument_uid": UID, "candles": [item]}})
        self.ns["VTB"] = SimpleNamespace(connection=connection)
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        bars = self.built[0][0]["structure_bars_by_timeframe"]["1h"]
        self.assertEqual(bars[-1]["close"], 12.735)
        self.assertEqual(bars[-1]["ts"], at.timestamp())
        self.assertEqual(bars[-1]["source_identity"]["contract_id"], UID)

    def test_missing_normalization_certificate_does_not_consume_history_refresh(self):
        at = NOW-timedelta(hours=1)
        item = {"time": at.isoformat(), "open": 12.73, "high": 12.74,
                "low": 12.72, "close": 12.735, "volume_lots": 200}
        self.ns["VTB"] = SimpleNamespace(connection=SimpleNamespace(lock=threading.RLock(), candles={
            ("CNYRUBF", "1h"): {"loaded_at": NOW.isoformat(), "status": "OK",
                                "instrument_uid": UID, "candles": [item]}}))
        captured = deepcopy(BR._markets["CNYRUBF"])
        captured["quote"]["contract"].pop("normalization_factor")
        self.runtime._native_broker_history(captured)
        self.assertNotIn(("CNYRUBF", "1h"), self.runtime._history_versions)
        captured["quote"]["contract"]["normalization_factor"] = 1.0
        updated = self.runtime._native_broker_history(captured)
        self.assertEqual(updated["structure_bars_by_timeframe"]["1h"][-1]["close"], 12.735)

    def test_later_cache_load_cannot_replace_a_newer_native_bar_with_older_history(self):
        captured = deepcopy(BR._markets["CNYRUBF"])
        before = deepcopy(captured["structure_bars_by_timeframe"])
        item = {"time": (NOW-timedelta(hours=5)).isoformat(), "open": 12.73, "high": 12.74,
                "low": 12.72, "close": 12.735, "volume_lots": 200}
        self.ns["VTB"] = SimpleNamespace(connection=SimpleNamespace(lock=threading.RLock(), candles={
            ("CNYRUBF", "1h"): {"loaded_at": NOW.isoformat(), "status": "OK",
                                "instrument_uid": UID, "candles": [item]}}))
        updated = self.runtime._native_broker_history(captured)
        self.assertEqual(updated["structure_bars_by_timeframe"], before)

    def test_new_event_drops_old_plan_veto_and_does_not_invent_confidence(self):
        template = {"research_decision": "SHORT", "confidence": .95,
                    "independent_evidence_families": 9, "hard_veto": True,
                    "_old_rule_veto": True, "trade_plan": {"hard_invalidation": True}}
        context = {"status": "OK", "event": {"direction": "LONG", "event_id": "new-event"}}

        def prepare(row, price=None, now=None):
            self.assertEqual(row["trade_plan"], {})
            self.assertNotIn("hard_veto", row)
            self.assertNotIn("_old_rule_veto", row)
            return dict(row, trade_plan={"eligible": True, "entry_price": price})

        with patch.object(BR.TFP, "prepare_row", side_effect=prepare), \
                patch.object(BR.TFP, "final_plan", side_effect=lambda asset, direction, plan, now: plan):
            row = self.runtime._row(BR._markets["CNYRUBF"], quote(), "1h", context, template, NOW)
        self.assertIsNone(row["confidence"])
        self.assertNotIn("independent_evidence_families", row)
        self.assertIsNone(row["pwin"])
        self.assertFalse(row["production_eligible"])

    def knowledge_template(self, at=None):
        return dict(quote(), created_at=(NOW-timedelta(seconds=30)).isoformat() if at is None else at,
                    knowledge_shadow_matches=[{
                        "version": "KNOWLEDGE_PROSPECTIVE_V1", "rule_key": "immutable-rule-key",
                        "rule_id": "RULE_1", "source_id": "SOURCE_1", "definition_hash": "d"*64,
                        "audit_hash": "a"*64, "registered_at": (NOW-timedelta(days=1)).isoformat(),
                        "action": "LONG", "contract_hash": "c"*64}])

    def knowledge_row(self, template, clock=NOW):
        context = {"status": "OK", "event": {"direction": "LONG", "event_id": "new-event"}}
        with patch.object(BR.TFP, "prepare_row", side_effect=lambda row, **kw: row), \
             patch.object(BR.TFP, "final_plan", side_effect=lambda asset, direction, plan, **kw: plan):
            return self.runtime._row(BR._markets["CNYRUBF"], quote(clock), "1h", context, template, clock)

    def test_recent_same_source_knowledge_preserves_every_frozen_hash_and_origin(self):
        template = self.knowledge_template()
        before = deepcopy(template)
        row = self.knowledge_row(template)
        self.assertEqual(row["knowledge_shadow_matches"], template["knowledge_shadow_matches"])
        self.assertEqual(row["knowledge_context_at"], template["created_at"])
        self.assertEqual(template, before)
        row["knowledge_shadow_matches"][0]["definition_hash"] = "changed"
        self.assertEqual(template, before)

    def test_knowledge_context_reuse_never_refreshes_the_original_analytical_time(self):
        template = self.knowledge_template(NOW.isoformat())
        first = self.knowledge_row(template)
        second = self.knowledge_row(first, NOW+timedelta(seconds=30))
        self.assertEqual(second["knowledge_context_at"], NOW.isoformat())
        self.assertEqual(second["knowledge_shadow_matches"], template["knowledge_shadow_matches"])
        exact = self.knowledge_row(second, NOW+timedelta(seconds=120))
        self.assertEqual(exact["knowledge_context_at"], NOW.isoformat())
        self.assertEqual(exact["knowledge_shadow_matches"], template["knowledge_shadow_matches"])
        stale = self.knowledge_row(exact, NOW+timedelta(seconds=121))
        self.assertEqual(stale["knowledge_shadow_matches"], [])
        self.assertIsNone(stale["knowledge_context_at"])

    def test_stale_future_missing_and_naive_knowledge_time_cannot_be_borrowed(self):
        for value in ((NOW-timedelta(seconds=121)).isoformat(), (NOW+timedelta(seconds=1)).isoformat(),
                      NOW.replace(tzinfo=None), NOW.replace(tzinfo=None).isoformat(), "", "bad", NOW.timestamp()):
            with self.subTest(time=value):
                template = self.knowledge_template(value)
                row = self.knowledge_row(template)
                self.assertEqual(row["knowledge_shadow_matches"], [])
                self.assertIsNone(row["knowledge_context_at"])
        template = self.knowledge_template(); template.pop("created_at")
        self.assertEqual(self.knowledge_row(template)["knowledge_shadow_matches"], [])
        # A future display timestamp cannot refresh an old analytical origin.
        template = self.knowledge_template(NOW.isoformat())
        template["knowledge_context_at"] = (NOW-timedelta(seconds=121)).isoformat()
        self.assertEqual(self.knowledge_row(template)["knowledge_shadow_matches"], [])

    def test_other_provider_or_contract_cannot_supply_knowledge_context(self):
        for mutate in (lambda row: row.update(source_names={"primary": "MOEX"}),
                       lambda row: row.update(contract={"instrument_uid": "other"}),
                       lambda row: row.pop("source_names")):
            template = self.knowledge_template(); mutate(template)
            row = self.knowledge_row(template)
            self.assertEqual(row["knowledge_shadow_matches"], [])

    def test_knowledge_carry_does_not_borrow_probability_permission_or_old_plan(self):
        template = self.knowledge_template()
        template.update(research_decision="SHORT", confidence=.99, pwin=.98,
                        calibration={"probability_correct": .97}, production_eligible=True,
                        orders_enabled=True, hard_veto=True, knowledge_rule_active=True,
                        gates={"source": False}, trade_plan={"old_permission": True})
        row = self.knowledge_row(template)
        self.assertEqual(len(row["knowledge_shadow_matches"]), 1)
        self.assertIsNone(row["pwin"])
        self.assertIsNone(row["confidence"])
        self.assertEqual(row["probability_source"], "STRUCTURAL_EVENT_UNCALIBRATED")
        self.assertFalse(row["production_eligible"])
        for key in ("calibration", "orders_enabled", "hard_veto", "knowledge_rule_active", "gates"):
            self.assertNotIn(key, row)
        self.assertNotIn("old_permission", row["trade_plan"])

    def test_knowledge_objects_are_bounded_and_malformed_context_is_refused(self):
        template = self.knowledge_template()
        match = template["knowledge_shadow_matches"][0]
        template["knowledge_shadow_matches"] = [dict(match, rule_id="RULE_"+str(i)) for i in range(20)]
        row = self.knowledge_row(template)
        self.assertEqual(row["knowledge_shadow_matches"], template["knowledge_shadow_matches"][:8])
        self.assertLessEqual(len(json.dumps(row["knowledge_shadow_matches"]).encode()), 8192)
        for bad in ("bad", [None], [{"rule_id": ""}], [dict(match, weight=float("nan"))],
                    [dict(match, data={"not": "a frozen scalar"})], [dict(match, extra="x"*8193)],
                    [dict(match, extra="x"*5000), dict(match, extra="x"*5000)]):
            with self.subTest(shape=str(type(bad))):
                template["knowledge_shadow_matches"] = bad
                self.assertEqual(self.knowledge_row(template)["knowledge_shadow_matches"], [])

    def test_fast_context_requests_only_display_limit_from_shared_state_owner(self):
        with patch.object(BR.TFD, "structural_context", return_value={"status": "OK"}) as owner:
            result = BR._bounded_context({"asset": "ETH"}, "5m", NOW)
        owner.assert_called_once_with({"asset": "ETH"}, "5m", NOW, level_limit=40)
        self.assertEqual(result, {"status": "OK"})

    def test_row_does_not_copy_discarded_display_levels(self):
        class DiscardedDisplayLevel:
            def __deepcopy__(self, memo):
                raise AssertionError("discarded display history was copied")
        retained = [{"level_id": str(i), "price": 12.7} for i in range(40)]
        context = {"status": "OK", "event": {"direction": "LONG", "event_id": "new-event"},
                   "levels": [DiscardedDisplayLevel(), *retained]}
        with patch.object(BR.TFP, "prepare_row", side_effect=lambda row, **kwargs: dict(row, trade_plan={})), \
                patch.object(BR.TFP, "final_plan", side_effect=lambda asset, direction, plan, now: plan):
            row = self.runtime._row(BR._markets["CNYRUBF"], quote(), "1h", context, {}, NOW)
        self.assertEqual(row["timeframe_entry_context"]["levels"], retained)
        retained[-1]["price"] = -1
        self.assertEqual(row["timeframe_entry_context"]["levels"][-1]["price"], 12.7)

    def test_context_and_callback_use_elapsed_wall_time_without_redating_quote(self):
        wall = [NOW]
        context_times, row_times, callbacks = [], [], []

        def build(raw, horizon, clock):
            context_times.append(clock)
            wall[0] += timedelta(seconds=121)
            return {"status": "OK", "timeframe": horizon,
                    "event": {"direction": "LONG", "signal_at": NOW.timestamp()}}

        def prepare(market, q, horizon, context, template, clock):
            row_times.append(clock)
            return dict(q, horizon=horizon, timeframe_entry_context=context)

        def execute(rows, clock):
            callbacks.append((clock, deepcopy(rows)))
            # The real canonical callback has the same independent clock gate.
            allowed = BR.VX.paper_quote_time_gate(rows[0], now=clock)["eligible"]
            return {"status": "OK" if allowed else "BLOCKED", "paper_only": True}

        self.runtime.context_builder = build
        self.runtime.entry_pass = execute
        with patch.object(BR, "_clock", side_effect=lambda value=None: wall[0] if value is None else value), \
                patch.object(self.runtime, "_row", side_effect=prepare):
            result = self.runtime.run_once(quotes={"CNYRUBF": quote()})
        self.assertEqual(context_times, [NOW])
        self.assertEqual(row_times, [NOW+timedelta(seconds=121)])
        self.assertEqual(callbacks[0][0], NOW+timedelta(seconds=121))
        self.assertEqual(callbacks[0][1][0]["observed_at"], NOW.isoformat())
        self.assertEqual(callbacks[0][1][0]["timeframe_entry_context"]["event"]["signal_at"], NOW.timestamp())
        self.assertEqual(result["execution"]["status"], "BLOCKED")
        self.assertEqual(result["context_builds"], 1)

    def test_actual_callback_result_is_published_without_removing_other_assets(self):
        self.ns["last_cycle"]["summary"] = [{"asset": "BTC", "horizon": "1h", "price": 80000}]
        row = dict(quote(), horizon="1h", research_decision="LONG",
                   market_observed_at=NOW.isoformat(), trade_plan={"eligible": False},
                   timeframe_entry_context={"event": {"event_id": "test-event"}})
        with patch.object(self.runtime, "_row", return_value=row):
            self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.assertEqual(len(self.executed), 1)
        summary = self.ns["last_cycle"]["summary"]
        self.assertEqual({r["asset"] for r in summary}, {"BTC", "CNYRUBF"})
        cny = next(r for r in summary if r["asset"] == "CNYRUBF")
        self.assertEqual(cny["_execution_audit"]["result"]["portfolios"][0]["reason"], "TEST_CALLBACK")
        slow = dict(row, market_observed_at=(NOW-timedelta(seconds=20)).isoformat())
        merged = BR.publish_summary([slow])
        self.assertEqual(merged[0]["market_observed_at"], NOW.isoformat())
        self.assertIn("_execution_audit", merged[0])

    def test_delayed_fast_permission_cannot_replace_later_full_refusal_or_redate_publication(self):
        denied = signal_row(checked=NOW+timedelta(seconds=5), eligible=False)
        denied["trade_plan"]["reason"] = "STRUCTURAL_EVENT_EXPIRED"
        previous_publication = (NOW-timedelta(seconds=1)).isoformat()
        self.ns["last_cycle"].update(summary=[denied], at=previous_publication,
                                     signals_updated_at=previous_publication)
        self.ns["now"] = lambda: self.fail("a discarded plan must not redate publication")
        for observed in (NOW, NOW-timedelta(seconds=1)):
            with self.subTest(observed=observed):
                ready = dict(signal_row(observed=observed), _breakout_runtime=BR.VERSION)
                self.runtime._publish_rows([ready], {"status": "BUSY"}, NOW)
                self.assertIs(self.ns["last_cycle"]["summary"][0], denied)
                self.assertEqual(BR._latest_rows[("CNYRUBF", "1h")], denied)
                self.assertEqual(BR.publish_summary([ready])[0], denied)
                self.assertEqual(self.ns["last_cycle"]["signals_updated_at"], previous_publication)
                self.assertEqual(self.ns["last_cycle"]["at"], previous_publication)

    def test_late_exact_fill_keeps_newer_refusal_and_updates_only_publication_clock(self):
        denied = signal_row(checked=NOW+timedelta(seconds=5), eligible=False)
        ready = dict(signal_row(), _breakout_runtime=BR.VERSION)
        completed_at = (NOW-timedelta(minutes=1)).isoformat()
        published_at = (NOW+timedelta(seconds=10)).isoformat()
        fill_at = NOW+timedelta(seconds=1)
        self.ns["last_cycle"].update(summary=[denied], at=completed_at)
        self.ns["now"] = lambda: published_at
        self.runtime._publish_rows([ready], filled_report(ready, fill_at), fill_at)
        current = self.ns["last_cycle"]["summary"][0]
        self.assertFalse(current["trade_plan"]["eligible"])
        self.assertEqual(current["trade_entry_checked_at"], denied["trade_entry_checked_at"])
        self.assertEqual(current["market_observed_at"], NOW.isoformat())
        self.assertEqual(current["_execution_audit"]["checked_at"], fill_at.isoformat())
        receipt = current["_execution_audit"]["result"]["portfolios"][0]
        self.assertEqual(receipt["order_id"], "actual-order")
        self.assertEqual(receipt["checked_at"], fill_at.isoformat())
        self.assertEqual(self.ns["last_cycle"]["signals_updated_at"], published_at)
        self.assertEqual(self.ns["last_cycle"]["at"], completed_at)
        self.assertEqual(BR._latest_rows[("CNYRUBF", "1h")], current)
        self.assertEqual(BR.publish_summary([ready])[0], current)
        self.ns["now"] = lambda: self.fail("a duplicate fill is not a new publication")
        self.runtime._publish_rows([ready], filled_report(ready, fill_at), fill_at)
        self.assertIs(self.ns["last_cycle"]["summary"][0], current)

    def test_fast_only_new_cell_updates_publication_without_touching_completed_cycle(self):
        ready = dict(signal_row(), _breakout_runtime=BR.VERSION)
        completed_at = (NOW-timedelta(minutes=1)).isoformat()
        published_at = (NOW+timedelta(seconds=10)).isoformat()
        self.ns["last_cycle"].update(at=completed_at, cycle_in_progress={"cycle_id": "in-flight"})
        self.ns["now"] = lambda: published_at
        self.runtime._publish_rows([ready], {"status": "BUSY"}, NOW)
        last = self.ns["last_cycle"]
        self.assertEqual(last["signals_updated_at"], published_at)
        self.assertEqual(last["at"], completed_at)
        self.assertEqual(last["cycle_in_progress"], {"cycle_id": "in-flight"})
        self.assertEqual(last["summary"][0]["market_observed_at"], NOW.isoformat())
        self.assertEqual(last["summary"][0]["trade_entry_checked_at"], NOW.isoformat())
        self.ns["now"] = lambda: self.fail("an identical fast row is not a new publication")
        self.runtime._publish_rows([deepcopy(ready)], {"status": "BUSY"}, NOW)
        self.assertEqual(last["signals_updated_at"], published_at)

    def test_cached_overlay_cannot_switch_canonical_source_or_contract(self):
        canonical = signal_row(eligible=False)
        foreign_contract = dict(signal_row(observed=NOW+timedelta(seconds=10), uid="other-contract"),
                                _breakout_runtime=BR.VERSION)
        foreign_source = dict(signal_row(observed=NOW+timedelta(seconds=10)),
                              _breakout_runtime=BR.VERSION, source_names={"primary": "MOEX ISS"})
        for cached in (foreign_contract, foreign_source):
            with self.subTest(cached=cached):
                BR._latest_rows[("CNYRUBF", "1h")] = deepcopy(cached)
                self.assertEqual(BR.publish_summary([canonical]), [canonical])
                self.ns["last_cycle"]["summary"] = [canonical]
                self.ns["now"] = lambda: self.fail("a foreign cached plan was published")
                self.runtime._publish_rows([], {"status": "OK"}, NOW)
                self.assertIs(self.ns["last_cycle"]["summary"][0], canonical)
                self.assertEqual(BR._latest_rows[("CNYRUBF", "1h")], canonical)

    def test_fast_full_fast_interleaving_cannot_restore_old_permission(self):
        ready = dict(signal_row(), _breakout_runtime=BR.VERSION)
        fill_at = NOW+timedelta(seconds=1)
        self.ns["now"] = lambda: (NOW+timedelta(seconds=10)).isoformat()
        self.runtime._publish_rows([ready], filled_report(ready, fill_at), fill_at)
        denied = signal_row(checked=NOW+timedelta(seconds=5), eligible=False)
        BR.VSP.publish_completed(self.ns, [denied], "later-full", "FULL")
        current = self.ns["last_cycle"]["summary"][0]
        self.assertFalse(current["trade_plan"]["eligible"])
        self.ns["now"] = lambda: self.fail("old fast work changed the later full refusal")
        self.runtime._publish_rows([ready], {"status": "BUSY"}, NOW)
        self.assertIs(self.ns["last_cycle"]["summary"][0], current)
        self.assertEqual(BR._latest_rows[("CNYRUBF", "1h")], current)
        self.assertEqual(BR.publish_summary([ready])[0], current)
        self.assertEqual(current["_execution_audit"]["result"]["portfolios"][0]["order_id"], "actual-order")

    def test_signal_report_ignores_large_book_graphs_and_unselected_events(self):
        class ArchivedProof:
            def __deepcopy__(self, memo):
                raise AssertionError("an unrelated archived proof must never be copied onto a signal")

        row = dict(quote(), horizon="5m", market_observed_at=NOW.isoformat(),
                   timeframe_entry_context={"event": {"event_id": "selected-event"}})
        selected = {"asset": "CNYRUBF", "horizon": "5m", "event_id": "selected-event",
                    "execution": {"status": "BLOCKED", "reason": "RECORDED_SOURCE_CHECK"},
                    "admission": {"prepared_plan": ArchivedProof()}}
        execution = {"status": "OK", "paper_only": True, "positions": ArchivedProof(),
                     "portfolios": [{"name": "Currency", "positions": ArchivedProof(),
                                     "admission_trace": [selected,
                                         dict(selected, horizon="1m"),
                                         dict(selected, event_id="previous-event"),
                                         dict(selected, asset="BTC")]}]}
        self.runtime._publish_rows([row], execution, NOW)
        report = row["_execution_audit"]["result"]
        self.assertEqual(len(report["portfolios"]), 1)
        self.assertEqual(report["portfolios"][0]["name"], "Currency")
        self.assertEqual(report["portfolios"][0]["event_id"], "selected-event")
        self.assertLess(len(json.dumps(report)), 1000)
        self.assertNotIn("positions", json.dumps(report))
        selected["execution"]["reason"] = "LATER_MUTATION"
        self.assertEqual(report["portfolios"][0]["reason"], "RECORDED_SOURCE_CHECK")
        before = deepcopy(report)
        execution["portfolios"][0]["admission_trace"].extend(
            dict(selected, asset="UNRELATED_" + str(i)) for i in range(1000))
        selected["execution"]["reason"] = "RECORDED_SOURCE_CHECK"
        self.runtime._publish_rows([row], execution, NOW)
        self.assertEqual(row["_execution_audit"]["result"], before)

    def test_display_projection_keeps_complete_sealed_signal_evidence(self):
        import veritas_structural_breakout as SB
        from test_veritas_structural_cny_episodes import episode_raw
        raw = episode_raw(1, "2026-10-07T07:15:00Z", 12.766)
        clock = datetime.fromisoformat(raw["observed_at"].replace("Z", "+00:00"))
        context = SB.build_context(raw, "5m", clock)
        row = self.runtime._row(raw, BR._clean_quote(raw), "5m", context, {}, clock)
        expected = deepcopy(row)
        event = context["event"]
        execution = {"status": "OK", "portfolios": [{"name": "Currency", "admission_trace": [
            {"asset": "CNYRUBF", "horizon": "5m", "event_id": event["event_id"],
             "execution": {"status": "BLOCKED", "reason": "REPORT_ONLY"}}]}]}
        self.runtime._publish_rows([row], execution, clock)
        cached = BR._latest_rows[("CNYRUBF", "5m")]
        for field in ("timeframe_entry_context", "trend_entry_context", "trade_plan", "_execution_quote"):
            self.assertEqual(row[field], expected[field], field)
            self.assertEqual(cached[field], expected[field], field)
        self.assertTrue(SB.validate_event(cached["trade_plan"]["entry_event_snapshot"],
                                        context["source_identity"])["eligible"])
        row["trade_plan"]["entry_event_snapshot"]["atr_proof"]["bars"][0]["close"] = -1.
        self.assertEqual(cached["trade_plan"], expected["trade_plan"])


if __name__ == "__main__":
    unittest.main()
