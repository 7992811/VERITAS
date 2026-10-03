"""Research signals must remain causal before they can be considered for paper."""
import copy
import unittest
import veritas_impulse_candidate as C
from test_veritas_minute_entry import data,NOW


class ImpulseCandidateTests(unittest.TestCase):
    def case(self):
        b,m=data();b[-24].update(high=103,low=97)
        return b,m,NOW.timestamp()

    def test_trigger_has_known_range_and_stop_preceding_signal_minute(self):
        b,m,now=self.case();e=C.early_event(b,m,'BTC',now)
        self.assertEqual(e['trigger_level'],100.5)
        self.assertEqual(e['signal_at'],now)
        self.assertLess(e['stop_price'],min(r['low'] for r in m[-4:-1]))
        changed=copy.deepcopy(m);changed[-1]['low']=99.7
        self.assertEqual(C.early_event(b,changed,'BTC',now)['stop_price'],e['stop_price'])

    def test_partial_future_bars_cannot_change_signal(self):
        b,m,now=self.case();e=C.early_event(b,m,'BTC',now)
        b.append(dict(b[-1],ts=now,high=999))
        m.append(dict(m[-1],ts=now,close=999,high=1000))
        self.assertEqual(C.early_event(b,m,'BTC',now),e)

    def test_low_volume_and_stale_minute_are_not_entries(self):
        b,m,now=self.case();m[-1]['volume']=50
        self.assertIsNone(C.early_event(b,m,'BTC',now))
        self.assertIsNone(C.early_event(b,m,'BTC',now+60))

    def test_short_is_symmetric(self):
        b,m,now=self.case()
        def reflect(r):return dict(r,open=200-r['open'],close=200-r['close'],high=200-r['low'],low=200-r['high'])
        long=C.early_event(b,m,'BTC',now)
        short=C.early_event([reflect(r) for r in b],[reflect(r) for r in m],'BTC',now)
        self.assertEqual(short['direction'],'SHORT')
        self.assertAlmostEqual(short['stop_price'],200-long['stop_price'])

    def test_first_retest_preserves_original_level_and_stop(self):
        b,m,now=self.case();event,pending=C.first_retest(b,m,'BTC',now,None)
        self.assertIsNone(event);old=dict(pending)
        m.append(dict(ts=now,open=100.5,high=100.7,low=100.49,close=100.65,volume=50))
        event,pending=C.first_retest(b,m,'BTC',now+60,pending)
        self.assertIsNone(pending)
        for key in ('event_id','trigger_level','stop_price','signal_at'):
            self.assertEqual(event[key],old[key])


if __name__=='__main__':unittest.main()
