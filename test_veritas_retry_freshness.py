"""Synthetic race regressions. No external connections or real orders."""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import Mock
import veritas_structural_validation_cache as SVC
import veritas_breakout_runtime as BR
import veritas_price_source as VPS
from test_veritas_structural_latency import BoundedRetryTests, context_row


class RetryOrderingTests(BoundedRetryTests):
    def test_retry_quote_is_published_but_not_marked_structurally_consumed(self):
        q=VPS.quote_from_row(self.row)
        source=BR._markets[self.row['asset']]['structure_source_identity']
        self.assertTrue(self.runtime._fresh_quote(self.row['asset'],source,q,self.clock))
        original=dict(self.runtime._observations)
        newer=deepcopy(q);newer['observed_at']=(self.clock+timedelta(seconds=1)).isoformat()
        newer['price']+=.001
        self.assertTrue(self.runtime._fresh_quote(self.row['asset'],source,newer,self.clock+timedelta(seconds=1),consume=False))
        self.assertEqual(self.runtime._observations,original)
        self.assertTrue(self.runtime._fresh_quote(self.row['asset'],source,newer,self.clock+timedelta(seconds=2)))
        self.assertNotEqual(self.runtime._observations,original)

    def test_source_rollover_discards_queue_without_callback(self):
        self.runtime._pending_entry_rows=[self.row]
        BR._markets[self.row['asset']]['structure_source_identity']=dict(
            BR._markets[self.row['asset']]['structure_source_identity'],contract_id='NEW_TEST_CONTRACT')
        result=self.runtime.run_once(self.clock,quotes={})
        self.runtime.entry_pass.assert_not_called()
        self.assertEqual(result['execution']['reason'],'STRUCTURAL_RETRY_SOURCE_CHANGED')
        self.assertEqual(self.runtime._pending_entry_rows,[])

    def test_removed_market_discards_queue_without_callback(self):
        self.runtime._pending_entry_rows=[self.row]
        BR._markets.clear()
        result=self.runtime.run_once(self.clock,quotes={})
        self.runtime.entry_pass.assert_not_called()
        self.assertEqual(result['execution']['status'],'BLOCKED')


class StaticMemoRaceTests(unittest.TestCase):
    def test_input_mutation_during_validation_is_not_cached(self):
        SVC.clear()
        event={'proof':'synthetic'}
        def validator(value,source):
            value['proof']='changed'
            return {'eligible':True,'reason':'STRUCTURAL_EVENT_PROOF_VALID'}
        SVC.verify(event,None,version='TEST',defaults={},validator=validator)
        self.assertEqual(SVC.snapshot()['entries'],0)
        reject=Mock(return_value={'eligible':False,'reason':'INVALID'})
        self.assertFalse(SVC.verify({'proof':'synthetic'},None,version='TEST',defaults={},validator=reject)['eligible'])
        reject.assert_called_once()
        SVC.clear()


if __name__=='__main__':unittest.main()
