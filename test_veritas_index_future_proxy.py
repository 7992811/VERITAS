import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_index_future_proxy as P
import veritas_tbank as T


NOW = datetime(2026, 10, 9, 18, 46, tzinfo=timezone.utc)


def contract(ticker, days=None, perpetual=False, uid=None):
    return {
        "uid": uid or ticker + "-uid", "ticker": ticker,
        "real_exchange": "REAL_EXCHANGE_MOEX", "perpetual": perpetual,
        "expiration_date": None if perpetual else (NOW + timedelta(days=days)).isoformat(),
        "basic_asset": "IMOEX", "name": "Индекс МосБиржи futures",
    }


class IndexFutureProxyTests(unittest.TestCase):
    def test_perpetual_has_priority_over_dated(self):
        out = P.choose([contract("MXZ6", 30), contract("IMOEXF", perpetual=True)], NOW)
        self.assertTrue(out["eligible"], out)
        self.assertEqual(out["ticker"], "IMOEXF")
        self.assertEqual(out["selection"], "PERPETUAL")

    def test_nearest_expiry_strictly_over_one_day_is_selected(self):
        out = P.choose([contract("MX1", .5), contract("MX2", 1.01), contract("MX3", 5)], NOW)
        self.assertTrue(out["eligible"], out)
        self.assertEqual(out["ticker"], "MX2")

    def test_one_day_or_less_is_never_a_new_entry_contract(self):
        for days in (0, .5, 1.0):
            with self.subTest(days=days):
                self.assertFalse(P.choose([contract("NEAR", days)], NOW)["eligible"])

    def test_wrong_exchange_fails_closed(self):
        bad = contract("WRONG", 10)
        bad["real_exchange"] = "REAL_EXCHANGE_OTHER"
        self.assertFalse(P.choose([bad], NOW)["eligible"])


class ResolverTests(unittest.TestCase):
    @staticmethod
    def future(uid, ticker, days=30):
        return {"instrument": {
            "uid": uid, "ticker": ticker, "lot": 1,
            "real_exchange": "REAL_EXCHANGE_MOEX", "exchange": "MOEX",
            "name": "Индекс МосБиржи futures", "basic_asset": "IMOEX",
            "expiration_date": (NOW + timedelta(days=days)).isoformat(),
            "min_price_increment": {"units": "1"},
            "min_price_increment_amount": {"units": "1"},
        }}

    def test_existing_imoexf_is_perpetual_execution_priority(self):
        class Reader:
            def call(self, name, **kw):
                if name == "find":
                    return {"instruments":[{"ticker":"IMOEXF","uid":"perp"}]} if kw["query"] == "IMOEXF" else {"instruments":[]}
                if name == "instrument":
                    return {"instrument":{"uid":"perp","ticker":"IMOEXF"}}
                if name == "future":
                    return ResolverTests.future("perp", "IMOEXF", 365)
                raise AssertionError((name, kw))
        with patch.object(T, "utcnow", return_value=NOW):
            selected = T.resolve_index_execution_future(Reader(), "IMOEXF")
        self.assertEqual(selected["ticker"], "IMOEXF")
        self.assertEqual(selected["execution_selection"], "PERPETUAL")
        self.assertEqual(selected["signal_asset"], "MOEX")
        self.assertEqual(selected["execution_asset"], "MOEXF")

    def test_fallback_skips_near_expiry_and_uses_next_contract(self):
        class Reader:
            def call(self, name, **kw):
                if name == "find":
                    if kw["query"] == "IMOEXF":
                        return {"instruments":[]}
                    if kw["query"] == "IMOEX":
                        return {"instruments":[
                            {"ticker":"MX_NEAR","uid":"near"},
                            {"ticker":"MX_NEXT","uid":"next"}]}
                    return {"instruments":[]}
                if name == "future":
                    return ResolverTests.future(kw["id"],
                        "MX_NEAR" if kw["id"] == "near" else "MX_NEXT",
                        .5 if kw["id"] == "near" else 10)
                raise AssertionError((name, kw))
        with patch.object(T, "utcnow", return_value=NOW):
            selected = T.resolve_index_execution_future(Reader(), "IMOEXF")
        self.assertEqual(selected["ticker"], "MX_NEXT")
        self.assertEqual(selected["execution_selection"], "NEAREST_EXPIRY_GT_1D")

    def test_missing_imoexf_keeps_legacy_not_found_diagnostic_if_discovery_is_empty(self):
        class Reader:
            def call(self, name, **kw):
                if name == "find":
                    return {"instruments":[]}
                raise AssertionError((name, kw))
        with patch.object(T, "utcnow", return_value=NOW):
            with self.assertRaisesRegex(T.TBankError, "INSTRUMENT_NOT_FOUND"):
                T.resolve_index_execution_future(Reader(), "IMOEXF")


if __name__ == "__main__":
    unittest.main()
