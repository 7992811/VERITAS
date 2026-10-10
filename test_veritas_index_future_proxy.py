import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_index_future_proxy as P
import veritas_tbank as T


NOW=datetime(2026,10,9,18,46,tzinfo=timezone.utc)


def contract(ticker, days=None, perpetual=False, uid=None):
    return {
        "uid":uid or ticker+"-uid","ticker":ticker,"real_exchange":"REAL_EXCHANGE_MOEX",
        "perpetual":perpetual,
        "expiration_date":None if perpetual else (NOW+timedelta(days=days)).isoformat(),
        "basic_asset":"IMOEX","name":"Индекс МосБиржи futures",
    }


class IndexFutureProxyTests(unittest.TestCase):
    def test_perpetual_has_priority_over_dated(self):
        result=P.choose([contract("MXZ6",30),contract("IMOEXF",perpetual=True)],NOW)
        self.assertTrue(result["eligible"],result)
        self.assertEqual(result["ticker"],"IMOEXF")
        self.assertEqual(result["selection"],"PERPETUAL")

    def test_nearest_expiry_strictly_over_one_day_is_selected(self):
        result=P.choose([contract("MX1",0.5),contract("MX2",1.01),contract("MX3",5)],NOW)
        self.assertTrue(result["eligible"],result)
        self.assertEqual(result["ticker"],"MX2")
        self.assertEqual(result["selection"],"NEAREST_EXPIRY_GT_1D")

    def test_one_day_or_less_is_never_new_entry_contract(self):
        for days in (0,0.5,1.0):
            with self.subTest(days=days):
                result=P.choose([contract("NEAR",days)],NOW)
                self.assertFalse(result["eligible"],result)

    def test_no_valid_future_fails_closed(self):
        bad=contract("WRONG",10)
        bad["real_exchange"]="REAL_EXCHANGE_OTHER"
        result=P.choose([bad],NOW)
        self.assertFalse(result["eligible"],result)


class ResolverTests(unittest.TestCase):
    def test_existing_perpetual_imoexf_is_used(self):
        reader=T.FakeReader() if hasattr(T,"FakeReader") else None
        # Unit-level metadata path: the selector contract is already proven by
        # the repository's TBank integration tests. Here freeze utcnow so the
        # >1 day fallback rule is deterministic.
        class Reader:
            def call(self,name,**kw):
                if name=="find":
                    q=kw["query"]
                    if q=="IMOEXF":
                        return {"instruments":[{"ticker":"IMOEXF","uid":"perp"}]}
                    return {"instruments":[]}
                if name=="instrument":
                    return {"instrument":{"uid":"perp","ticker":"IMOEXF","lot":1}}
                if name=="future":
                    return {"instrument":{"uid":"perp","ticker":"IMOEXF","lot":1,
                        "real_exchange":"REAL_EXCHANGE_MOEX","exchange":"MOEX",
                        "name":"IMOEX perpetual","expiration_date":(NOW+timedelta(days=365)).isoformat(),
                        "min_price_increment":{"units":"1"},"min_price_increment_amount":{"units":"1"},
                        "basic_asset":"IMOEX"}}
                raise AssertionError(name)
        with patch.object(T,"utcnow",return_value=NOW):
            selected=T.resolve_index_execution_future(Reader(),"IMOEXF")
        self.assertEqual(selected["ticker"],"IMOEXF")
        self.assertEqual(selected["execution_selection"],"PERPETUAL")
        self.assertEqual(selected["signal_asset"],"MOEX")
        self.assertEqual(selected["execution_asset"],"MOEXF")

    def test_fallback_ignores_near_expiry_and_uses_next_contract(self):
        class Reader:
            def call(self,name,**kw):
                if name=="find":
                    q=kw["query"]
                    if q=="IMOEXF":
                        return {"instruments":[]}
                    if q=="IMOEX":
                        return {"instruments":[
                            {"ticker":"MX_NEAR","uid":"near"},
                            {"ticker":"MX_NEXT","uid":"next"}]}
                    return {"instruments":[]}
                if name=="future":
                    uid=kw["id"]
                    days=.5 if uid=="near" else 10
                    ticker="MX_NEAR" if uid=="near" else "MX_NEXT"
                    return {"instrument":{"uid":uid,"ticker":ticker,"lot":1,
                        "real_exchange":"REAL_EXCHANGE_MOEX","exchange":"MOEX",
                        "name":"Индекс МосБиржи futures",
                        "expiration_date":(NOW+timedelta(days=days)).isoformat(),
                        "min_price_increment":{"units":"1"},"min_price_increment_amount":{"units":"1"},
                        "basic_asset":"IMOEX"}}
                raise AssertionError((name,kw))
        with patch.object(T,"utcnow",return_value=NOW):
            selected=T.resolve_index_execution_future(Reader(),"IMOEXF")
        self.assertEqual(selected["ticker"],"MX_NEXT")
        self.assertEqual(selected["execution_selection"],"NEAREST_EXPIRY_GT_1D")


if __name__=="__main__":
    unittest.main()
