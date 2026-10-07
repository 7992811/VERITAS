"""Queued quote renewal retains original event age and observed barriers."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock,patch
import veritas_structural_breakout as SB
import veritas_structural_lifecycle as SL
import veritas_position_guard as G
import veritas_timeframe_data as TFD
from test_veritas_structural_latency import context_row

class PendingQuoteTests(unittest.TestCase):
    def setUp(self):
        self.row,self.clock=context_row()
        with TFD._STRUCTURAL_LOCK:
            self.previous=deepcopy(TFD._STRUCTURAL_STATE);TFD._STRUCTURAL_STATE.clear()
    def tearDown(self):
        with TFD._STRUCTURAL_LOCK:
            TFD._STRUCTURAL_STATE.clear();TFD._STRUCTURAL_STATE.update(self.previous)
    def check(self,delta,*,foreign=False,stale=False,target=False):
        clock=self.clock+timedelta(seconds=delta)
        quote=G.VPS.quote_from_row(self.row)
        fresh=deepcopy(quote)
        if not stale:fresh['observed_at']=clock.isoformat()
        if foreign:fresh['contract']['instrument_uid']='OTHER'
        if target:fresh['price']=self.row['timeframe_entry_context']['event']['target_price']
        supplied=dict(self.row,_execution_quote=fresh)
        mutex=SimpleNamespace(acquire=Mock(return_value=False),reserve_entry_turn=Mock(),cancel_entry_turn=Mock())
        connect=Mock(side_effect=AssertionError('no database during busy renewal'))
        original=deepcopy(self.row)
        with patch.dict(sys.modules,{'veritas_portfolio_runtime':SimpleNamespace()}),patch.object(G,'_mutex',mutex),patch.object(G,'refresh_execution_row',return_value=supplied) as refresh,patch.object(SL,'_wall_clock',return_value=clock):
            result=SL.fast_entry_pass({'pg_connect':connect},[self.row],self.clock,runtime=True)
        connect.assert_not_called();refresh.assert_called_once_with(self.row,now=clock)
        self.assertEqual(self.row,original)
        return result,mutex
    def test_original_quote_can_age_out_while_new_quote_keeps_original_event_ready(self):
        result,mutex=self.check(121)
        self.assertTrue(result['entry_turn_reserved']);mutex.reserve_entry_turn.assert_called_once()
    def test_stale_quote_cannot_renew(self):
        result,mutex=self.check(121,stale=True)
        self.assertFalse(result['entry_turn_reserved']);mutex.cancel_entry_turn.assert_called_once()
    def test_foreign_contract_cannot_renew(self):
        result,_=self.check(121,foreign=True);self.assertFalse(result['entry_turn_reserved'])
    def test_new_quote_cannot_renew_expired_original_event(self):
        result,_=self.check(7201);self.assertFalse(result['entry_turn_reserved'])
    def test_target_reached_while_busy_cancels_entry(self):
        result,_=self.check(121,target=True);self.assertFalse(result['entry_turn_reserved'])
if __name__=='__main__':unittest.main()
