from datetime import datetime, timezone
import time
import unittest
from unittest.mock import patch

import veritas_intelligence as VI


class GoldProFinancePrimaryTests(unittest.TestCase):
    def setUp(self):
        VI._v90r63_futures_history_cache.pop("GOLD",None)
        now=time.time()-60
        self.hourly=[{"ts":now-3600*(219-i),"open":4180.0,"high":4185.0,"low":4175.0,
                      "close":4180.0+i*0.01,"volume":100.0} for i in range(220)]
        self.five=[{"ts":now-300*(79-i),"open":4180.0,"high":4182.0,"low":4178.0,
                    "close":4180.0+i*0.01,"volume":50.0} for i in range(80)]
        self.proxy=[{"ts":x["ts"],"open":100.0,"high":101.0,"low":99.0,
                    "close":100.0,"volume":100.0} for x in self.five]

    def _series(self,symbol,range_,interval,*args,**kwargs):
        if interval=="1h":
            return list(self.hourly),{}
        if symbol=="GLD":
            return list(self.proxy),{}
        return list(self.five),{}

    def test_profinance_wins_even_when_stooq_is_fresher(self):
        now=datetime.now(timezone.utc)
        pf={"price":4152.7,"observed_at":now.isoformat(),"source":"ProFinance","raw_label":"Gold"}
        st={"ok":True,"price":4153.0,"observed_at":now.isoformat()}
        with patch.object(VI,"_yahoo_series",side_effect=self._series), \
             patch.object(VI,"_v90r61_profinance_quote",return_value=pf), \
             patch.object(VI,"_v90_stooq_public_quote",return_value=st):
            out=VI._yahoo_research_futures_market("GOLD","GC%3DF","GLD","yahoo_gold","Yahoo Gold GC=F")
        self.assertTrue(out["source_gate_pass"],out)
        self.assertEqual(out["verification_mode"],"PUBLIC_DIRECT_FUTURES_PAPER")
        self.assertEqual(out["source_names"]["primary"],"ProFinance")
        self.assertAlmostEqual(out["price"],4152.7)

    def test_no_profinance_means_no_gold_execution_fallback(self):
        now=datetime.now(timezone.utc)
        st={"ok":True,"price":4153.0,"observed_at":now.isoformat()}
        with patch.object(VI,"_yahoo_series",side_effect=self._series), \
             patch.object(VI,"_v90r61_profinance_quote",return_value={}), \
             patch.object(VI,"_v90_stooq_public_quote",return_value=st):
            out=VI._yahoo_research_futures_market("GOLD","GC%3DF","GLD","yahoo_gold","Yahoo Gold GC=F")
        self.assertFalse(out["source_gate_pass"],out)
        self.assertEqual(out["verification_mode"],"PROFINANCE_PRIMARY_UNAVAILABLE")
        self.assertEqual(out["source_names"]["primary"],"Yahoo GOLD context only")


if __name__=="__main__":
    unittest.main()
