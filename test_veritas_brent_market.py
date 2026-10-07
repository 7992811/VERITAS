"""New Brent candidates have one price authority; held positions keep theirs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

import veritas_brent_market as B
import veritas_position_guard as G
import veritas_price_source as VPS
import veritas_timeframe_structure as TS


NOW = datetime(2026, 10, 6, 12, 0, 20, tzinfo=timezone.utc)
IDENTITY = VPS.brent_feed_pin_identity()


def quote(**overrides):
    return {"price": 101.03, "observed_at": NOW.isoformat(), "source": "ProFinance",
            "raw_label": "Brent oil", "raw_ticker": "brent", "instrument_id": "27",
            "provider_ticker_verified": True, **overrides}


def candle(tf, stamp, close=100.):
    return {"ts": stamp, "end_ts": stamp + TS.TIMEFRAMES[tf],
            "available_at": stamp + TS.TIMEFRAMES[tf],
            "open": close - .1, "high": close + .2, "low": close - .2, "close": close,
            "volume": None, "volume_available": False, "timeframe": tf,
            "source": "ProFinance Brent oil", "source_identity": deepcopy(IDENTITY),
            "raw_label": "Brent oil", "chart_symbol": "brent", "price_type": "Last",
            "finalized": True}


def history(count=60):
    mapping = {}
    for tf in B.NATIVE_TIMEFRAMES:
        seconds = TS.TIMEFRAMES[tf]
        end = int(NOW.timestamp() // seconds) * seconds
        mapping[tf] = [candle(tf, end - (count-i)*seconds, 100. + .001*i)
                       for i in range(count)]
    return {"asset": "BRENT", "raw_label": "Brent oil", "source_identity": deepcopy(IDENTITY),
            "raw_ticker": "brent", "provider_chart_identity_verified": True,
            "bars_by_timeframe": mapping, "status_by_timeframe": {}}


class BrentNativeSourceTests(TestCase):
    def test_all_timeframes_and_forecast_arrays_use_same_native_source(self):
        h = history()
        r = B.build_market(quote(), h, NOW)
        self.assertTrue(r["source_gate_pass"])
        self.assertTrue(r["paper_eligible"])
        self.assertFalse(r["production_eligible"])
        self.assertNotIn("contract", r)
        self.assertEqual(r["source_names"]["primary"], "ProFinance")
        self.assertEqual(r["closes"], [b["close"] for b in h["bars_by_timeframe"]["1h"]])
        for tf, bars in r["structure_bars_by_timeframe"].items():
            self.assertEqual(len(bars), 60, tf)
            self.assertTrue(all(b["source_identity"]["key"] == "PROFINANCE:Brent oil" for b in bars))
            self.assertTrue(all(b["timeframe"] == tf for b in bars))

    def test_quote_never_rewrites_last_native_bar(self):
        h = history()
        before = deepcopy(h)
        q = quote(price=104.)
        r = B.build_market(q, h, NOW)
        self.assertEqual(h, before)
        self.assertEqual(r["price"], 104.)
        self.assertEqual(r["closes"][-1], 100.059)
        self.assertNotEqual(r["hourly_bars"][-1]["close"], r["price"])

    def test_missing_volume_stays_unobserved_in_every_native_timeframe(self):
        r = B.build_market(quote(), history(), NOW)
        self.assertFalse(r["volume_available"])
        self.assertTrue(all(v == 0. for v in r["vols"]))
        for bars in r["structure_bars_by_timeframe"].values():
            self.assertTrue(all(b["volume"] is None and b["volume_available"] is False for b in bars))

    def test_old_quote_is_not_refreshed_by_current_history(self):
        obs = (NOW - timedelta(minutes=15)).isoformat()
        r = B.build_market(quote(observed_at=obs), history(), NOW)
        self.assertFalse(r["source_gate_pass"])
        self.assertFalse(r["paper_eligible"])
        self.assertEqual(r["observed_at"], obs)
        self.assertEqual(r["brent_source_gate_reason"], "EXECUTION_QUOTE_STALE")
        self.assertEqual(r["quote_time_gate"]["max_age_seconds"], 120)

    def test_missing_quote_can_only_display_same_source_history_reference(self):
        r = B.build_market({}, history(), NOW)
        self.assertFalse(r["source_gate_pass"])
        self.assertFalse(r["paper_eligible"])
        self.assertIsNone(r["observed_at"])
        self.assertIsNone(r["structure_quote"]["price"])
        self.assertEqual(r["reference_price_basis"], "NATIVE_HISTORY_CONTEXT_ONLY")
        self.assertEqual(r["brent_source_gate_reason"], "BRENT_PROFINANCE_QUOTE_UNAVAILABLE")

    def test_moex_or_other_label_never_becomes_a_profinance_quote(self):
        for q in (quote(source="MOEX ISS BRX6", contract={"secid":"BRX6"}),
                  quote(raw_label="WTI"), quote(contract={"secid":"BRZ6"})):
            r = B.build_market(q, history(), NOW)
            self.assertFalse(r["paper_eligible"])
            self.assertIsNone(r["observed_at"])

    def test_invalid_prices_cannot_be_executed(self):
        for price in (None, 0, -1, float("nan"), float("inf"), True, "invalid"):
            with self.subTest(price=price):
                r = B.build_market(quote(price=price), history(), NOW)
                self.assertFalse(r["paper_eligible"])
                self.assertIsNone(r["structure_quote"]["price"])

    def test_future_and_naive_quote_times_are_ineligible(self):
        for obs in ((NOW + timedelta(seconds=6)).isoformat(), NOW.replace(tzinfo=None).isoformat()):
            r = B.build_market(quote(observed_at=obs), history(), NOW)
            self.assertFalse(r["paper_eligible"])

    def test_history_from_another_contract_is_not_relabelled(self):
        h = history()
        h["source_identity"] = VPS.identity("BRENT", {"source":"MOEX ISS BRX6", "contract_id":"BRX6"})
        r = B.build_market(quote(), h, NOW)
        self.assertFalse(r["source_gate_pass"])
        self.assertEqual(r["structure_bars_by_timeframe"], {})
        self.assertEqual(r["brent_source_gate_reason"], "BRENT_HISTORY_SOURCE_MISMATCH")

    def test_wrong_source_or_timeframe_on_a_bar_is_removed(self):
        h = history()
        h["bars_by_timeframe"]["5m"][0]["source_identity"] = {"key":"YAHOO:BZ=F"}
        h["bars_by_timeframe"]["5m"][1]["timeframe"] = "1h"
        r = B.build_market(quote(), h, NOW)
        self.assertEqual(len(r["structure_bars_by_timeframe"]["5m"]), 58)
        self.assertEqual(len(r["structure_bars_by_timeframe"]["1h"]), 60)

    def test_history_requires_complete_pin_even_when_legacy_source_name_matches(self):
        old = VPS.identity("BRENT", {"source": "ProFinance", "raw_label": "Brent oil"})
        h = history()
        h["source_identity"] = old
        self.assertFalse(B.build_market(quote(), h, NOW)["paper_eligible"])
        h = history()
        h["bars_by_timeframe"]["5m"][0]["source_identity"] = old
        result = B.build_market(quote(), h, NOW)
        self.assertEqual(len(result["structure_bars_by_timeframe"]["5m"]), 59)

    def test_incomplete_and_conflicting_duplicate_bars_are_excluded(self):
        h = history()
        rows = h["bars_by_timeframe"]["1m"]
        rows.append(candle("1m", int(NOW.timestamp() // 60) * 60, 103.))
        duplicate = deepcopy(rows[0])
        duplicate.update(close=101., high=101.2)
        rows.append(duplicate)
        r = B.build_market(quote(), h, NOW)
        self.assertEqual(len(r["structure_bars_by_timeframe"]["1m"]), 59)
        self.assertTrue(all(b["end_ts"] <= NOW.timestamp() for b in r["structure_minute_bars"]))


    def test_daily_evidence_preserves_future_revision_but_not_foreign_source(self):
        h = history()
        future = h["bars_by_timeframe"]["1d"][-1]
        future.update(available_at=NOW.timestamp()+10, revision_observed_at=NOW.timestamp()+10)
        h["bars_by_timeframe"]["1d"][0]["source_identity"] = {"key":"YAHOO:BZ=F"}
        before = deepcopy(h)
        r = B.build_market(quote(), h, NOW)
        self.assertEqual(len(r["native_daily_evidence"]),59)
        self.assertEqual(len(r["structure_bars_by_timeframe"]["1d"]),58)
        self.assertEqual(r["native_daily_evidence"][-1]["revision_observed_at"],NOW.timestamp()+10)
        self.assertEqual(h,before)
        r["native_daily_evidence"][-1]["close"] = 999
        self.assertEqual(h,before)

    def test_rejected_history_does_not_forward_daily_evidence(self):
        h = history()
        h["source_identity"] = {"key":"YAHOO:BZ=F"}
        r = B.build_market(quote(),h,NOW)
        self.assertEqual(r["native_daily_evidence"],[])

    def test_no_data_is_explicit_unavailability(self):
        h = history(count=0)
        with self.assertRaisesRegex(RuntimeError, "BRENT_PROFINANCE_DATA_UNAVAILABLE"):
            B.build_market({}, h, NOW)

    def test_invalid_decision_time_cannot_become_now(self):
        for value in (False, "bad", NOW.replace(tzinfo=None), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    B.build_market(quote(), history(), value)

    def test_native_row_count_is_bounded(self):
        r = B.build_market(quote(), history(count=600), NOW)
        self.assertEqual(len(r["hourly_bars"]), 500)
        self.assertEqual(len(r["structure_minute_bars"]), 500)

    def test_fetch_refreshes_quote_after_bounded_history(self):
        order = []
        def fetch_history(**kwargs):
            order.append(("history", kwargs))
            return history()
        def fetch_quote(asset):
            order.append(("quote", asset))
            return quote()
        r = B.fetch_market(fetch_quote, fetch_history, NOW)
        self.assertEqual(order, [("history", {"asset":"BRENT", "now":NOW}), ("quote","BRENT")])
        self.assertTrue(r["paper_eligible"])
        self.assertTrue(r["native_source_history_attached"])

    def test_quote_failure_does_not_call_an_alternative_provider(self):
        q = Mock(side_effect=TimeoutError("provider timeout"))
        h = Mock(return_value=history())
        r = B.fetch_market(q, h, NOW)
        q.assert_called_once_with("BRENT")
        h.assert_called_once_with(asset="BRENT", now=NOW)
        self.assertFalse(r["paper_eligible"])
        self.assertEqual(r["source_names"]["primary"], "ProFinance")

    def _breakout_with_observed_partial(self, hit):
        h = history()
        step = 300
        signal_at = int(NOW.timestamp()//step)*step
        begin = signal_at-33*step
        rows = [candle("5m", begin+i*step, 100.) for i in range(32)]
        for row in rows:
            row.update(open=100., high=100.5, low=99.5, close=100.)
        rows[24]["high"] = 101.
        rows[27]["low"] = 99.
        rows.append({**candle("5m", begin+32*step, 101.2),
                     "open":100., "high":101.3, "low":100., "close":101.2})
        h["bars_by_timeframe"]["5m"] = rows
        context = TS.build_context(rows, "5m", NOW, asset="BRENT", source_identity=IDENTITY)
        self.assertTrue(TS.entry_gate(context, 101.2, "LONG", NOW)["eligible"], context)
        event = context["event"]
        partial = {**candle("5m", signal_at, 101.2),
                   "open":101.2, "low":101., "high":101.3,
                   "finalized":False, "observed_at":NOW.timestamp()}
        if hit == "STOP":
            partial["low"] = event["stop_price"]-.05
        else:
            partial["high"] = event["target_price"]+.05
        h["forming_bars_by_timeframe"] = {"5m":[partial]}
        return h, event

    def test_observed_partial_stop_or_target_spends_event_even_after_quote_returns(self):
        import veritas_timeframe_data as TFD
        for hit in ("STOP", "TARGET"):
            with self.subTest(hit=hit):
                h, original_event = self._breakout_with_observed_partial(hit)
                before = deepcopy(h)
                raw = B.build_market(quote(price=101.2), h, NOW)
                self.assertEqual(h, before)
                self.assertEqual(len(raw["structure_bars_by_timeframe"]["5m"]), 33)
                self.assertEqual(len(raw["structure_forming_bars_by_timeframe"]["5m"]), 1)
                with patch("veritas_native_daily.fetch_native_daily", return_value={"bars":[]}), \
                     patch("veritas_profinance_history.fetch_history_bundle") as repeated_fetch:
                    attached = TFD.attach(raw, NOW)
                repeated_fetch.assert_not_called()
                context = TS.build_context(attached["structure_bars_by_timeframe"]["5m"],
                                           "5m", NOW, asset="BRENT", source_identity=IDENTITY)
                self.assertEqual(context["bars"], 33)
                self.assertEqual(context["event"]["event_id"], original_event["event_id"])
                gate = TS.entry_gate(context, 101.2, "LONG", NOW)
                self.assertFalse(gate["eligible"])
                self.assertEqual(gate["reason"], "SAME_TF_"+hit+"_ALREADY_REACHED")

    def test_invalid_or_unobserved_partials_cannot_supply_invalidation_evidence(self):
        h, _ = self._breakout_with_observed_partial("STOP")
        alterations = [
            {"observed_at":None}, {"observed_at":NOW.timestamp()+1},
            {"finalized":True}, {"synthetic":True}, {"raw_label":"WTI"},
            {"timeframe":"1h"}, {"source_identity":{"key":"MOEX:BRENT"}},
            {"high":99.}, {"low":float("nan")}, {"price_type":"Bid"},
        ]
        for alteration in alterations:
            with self.subTest(alteration=alteration):
                modified = deepcopy(h)
                modified["forming_bars_by_timeframe"]["5m"][0].update(alteration)
                raw = B.build_market(quote(price=101.2), modified, NOW)
                self.assertEqual(raw["structure_forming_bars_by_timeframe"]["5m"], [])

    def test_conflicting_partial_observations_are_excluded(self):
        h, _ = self._breakout_with_observed_partial("TARGET")
        duplicate = deepcopy(h["forming_bars_by_timeframe"]["5m"][0])
        duplicate["high"] += .01
        h["forming_bars_by_timeframe"]["5m"].append(duplicate)
        raw = B.build_market(quote(price=101.2), h, NOW)
        self.assertEqual(raw["structure_forming_bars_by_timeframe"]["5m"], [])



class BrentHeldSourceTests(TestCase):
    def setUp(self):
        self.enterContext(patch.dict(G._quotes, {}, clear=True))
        self.enterContext(patch.dict(G._source_quotes, {}, clear=True))

    def position(self):
        identity = VPS.identity("BRENT", {"source":"MOEX ISS BRX6", "contract_id":"BRX6"})
        return {"asset":"BRENT", "direction":"LONG", "avg_entry_price":100.5, "last_price":100.5,
                "stop_price":99., "payload":{"price_source_lock":identity,
                    "entry_contract_secid":"BRX6",
                    "contract_identity":{"primary_source":"MOEX ISS BRX6", "contract_id":"BRX6"}}}

    def test_profinance_candidate_does_not_replace_saved_moex_position_mark(self):
        old = {"price":101.28, "observed_at":NOW.isoformat(), "source_gate_pass":True,
               "contract":{"secid":"BRX6"}, "source_names":{"primary":"MOEX ISS BRX6"}}
        pf = dict(quote(price=98.26), source_gate_pass=True,
                  source_names={"primary":"ProFinance"})
        G.publish_quote("BRENT", old)
        G.publish_quote("BRENT", pf)
        self.assertEqual(G.quote_for_position(self.position(), pf, NOW)["price"], 101.28)
        self.assertFalse(VPS.matches(self.position(), pf))

    def test_missing_saved_moex_quote_never_falls_back_to_profinance(self):
        pf = dict(quote(price=98.26), source_gate_pass=True,
                  source_names={"primary":"ProFinance"})
        G.publish_quote("BRENT", pf)
        self.assertEqual(G.quote_for_position(self.position(), pf, NOW), {})
        self.assertEqual(VPS.frozen_price(self.position()), 100.5)

    def test_protective_fetch_keeps_the_original_moex_contract(self):
        observed = datetime.now(timezone.utc).isoformat()
        moex = Mock(return_value={"price":101.28, "observed_at":observed})
        pf = Mock(return_value={})
        ns = {"_moex_futures_current_quote":moex, "_v90r61_profinance_quote":pf}
        result = G.fetch_guard_quote(ns, "BRENT", [self.position()])
        moex.assert_called_once_with("BRX6")
        pf.assert_not_called()
        self.assertEqual(result["contract"]["secid"], "BRX6")
        self.assertEqual(result["price"], 101.28)
