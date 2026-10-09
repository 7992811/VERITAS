"""Queued quote renewal retains original event age and observed barriers."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock,patch
import veritas_structural_breakout as SB
import veritas_structural_lifecycle as SL
import veritas_position_guard as G
import veritas_timeframe_data as TFD
import veritas_timeframe_policy as TFP
from veritas_book_lock import PriorityRLock
from test_veritas_structural_breakout import raw_at, POLICY
from test_veritas_structural_latency import context_row


class BusyDatabase:
    """No accounting SQL: exercise the real transaction's advisory-lock refusal."""
    def __init__(self):
        self.closed = False
        self.transaction_open = False
        self.statements = []
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        self.closed = True
    @contextmanager
    def transaction(self):
        self.transaction_open = True
        try:
            yield
        finally:
            self.transaction_open = False
    def execute(self, sql, params=()):
        self.statements.append(sql)
        if not (sql.startswith('SET LOCAL ') or sql.startswith('SELECT pg_try_advisory_xact_lock')):
            raise AssertionError('Accounting SQL forbidden during busy retry')
        return SimpleNamespace(fetchone=lambda: {'acquired': False})


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
        mutex=SimpleNamespace(acquire=Mock(return_value=False),reserve_entry_turn=Mock(),
                              cancel_entry_turn=Mock(),snapshot=Mock(return_value={'ordinary_waiters':0}))
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

    def test_each_queued_barrier_is_observed_before_reserving_and_stays_spent(self):
        # Both feeds, prices, dates and identities come from synthetic fixtures.
        rows = []
        for asset in ('CNYRUBF', 'GOLD'):
            raw, start = raw_at(asset=asset)
            context = SB.build_context(raw, '1h', start, config=POLICY)
            self.assertTrue(SB.validate_event(context['event'])['eligible'])
            rows.append(dict(raw, horizon='1h', research_decision='LONG',
                             timeframe_entry_context=context, trade_plan={}))
        for lane in ('LOCAL', 'DATABASE'):
            for mode in ('target', 'stop', 'foreign', 'stale', 'expired'):
                with self.subTest(lane=lane, mode=mode):
                    clock = start + timedelta(seconds=7201 if mode == 'expired' else 1)
                    with TFD._STRUCTURAL_LOCK:
                        TFD._STRUCTURAL_STATE.clear()
                    for row in rows:
                        self.assertTrue(TFD.restore_context_state(row))
                    owner_before = deepcopy(TFD._STRUCTURAL_STATE)
                    before, called, database = deepcopy(rows), [], BusyDatabase()
                    mutex = (PriorityRLock() if lane == 'DATABASE' else SimpleNamespace(
                        acquire=Mock(return_value=False), reserve_entry_turn=Mock(),
                        cancel_entry_turn=Mock(), snapshot=Mock(return_value={'ordinary_waiters':0})))

                    def current(row, now=None):
                        self.assertEqual(now, clock)
                        if lane == 'DATABASE':
                            self.assertTrue(database.closed)
                            self.assertFalse(database.transaction_open)
                            self.assertEqual(mutex.snapshot()['depth'], 0)
                        called.append(row['asset'])
                        quote = deepcopy(G.VPS.quote_from_row(row))
                        quote['observed_at'] = clock.isoformat()
                        if row['asset'] == 'GOLD' and mode != 'expired':
                            event = row['timeframe_entry_context']['event']
                            quote['price'] = event['stop_price'] if mode == 'stop' else event['target_price']
                            quote.update(best_bid=quote['price'], best_ask=quote['price'] + .01)
                            if mode == 'foreign':
                                quote['contract']['instrument_uid'] = 'SYNTHETIC-FOREIGN-UID'
                            if mode == 'stale':
                                quote['observed_at'] = (clock-timedelta(seconds=300)).isoformat()
                        return dict(row, _execution_quote=quote)

                    reserve = mutex.reserve_entry_turn
                    def reserve_after_observations(*args, **kwargs):
                        self.assertEqual(called, ['CNYRUBF', 'GOLD'])
                        return reserve(*args, **kwargs)

                    connect = (Mock(return_value=database) if lane == 'DATABASE' else
                               Mock(side_effect=AssertionError('Local busy must not open a database')))
                    with patch.dict(sys.modules, {'veritas_portfolio_runtime': SimpleNamespace()}), \
                            patch.object(G, '_mutex', mutex), \
                            patch.object(G, 'refresh_execution_row', side_effect=current), \
                            patch.object(mutex, 'reserve_entry_turn', side_effect=reserve_after_observations), \
                            patch.object(SL, '_wall_clock', return_value=clock):
                        result = SL.fast_entry_pass({'pg_connect': connect}, rows, start, runtime=True)
                    self.assertEqual(called, ['CNYRUBF', 'GOLD'])
                    self.assertEqual(result['reason'], lane + '_PAPER_BOOK_BUSY')
                    self.assertIs(result['entry_turn_reserved'], mode != 'expired')
                    self.assertEqual(rows, before)
                    gold = rows[1]
                    quote = deepcopy(G.VPS.quote_from_row(gold))
                    quote['observed_at'] = (clock+timedelta(seconds=1)).isoformat()
                    # The final retry of the original event sees the retreated
                    # price but must retain the owner's earlier stop/target fact.
                    gate = TFP.prepare_row(dict(gold, _execution_quote=quote),
                        now=clock+timedelta(seconds=1))['trade_plan']['entry_timing_gate']
                    if mode in ('target', 'stop', 'expired'):
                        self.assertFalse(gate['eligible'])
                        expected = {'target':'STRUCTURAL_TARGET_ALREADY_REACHED',
                                    'stop':'STRUCTURAL_STOP_ALREADY_REACHED',
                                    'expired':'STRUCTURAL_EVENT_EXPIRED'}
                        self.assertEqual(gate['reason'], expected[mode])
                    else:
                        self.assertTrue(gate['eligible'], gate)
                        self.assertEqual(TFD._STRUCTURAL_STATE, owner_before)
                    for key, state in TFD._STRUCTURAL_STATE.items():
                        self.assertEqual(state['last_quote'], owner_before[key]['last_quote'])
                    if lane == 'DATABASE':
                        self.assertEqual(len(database.statements), 2)
                        self.assertEqual(mutex.snapshot()['depth'], 0)
                        self.assertEqual(mutex.snapshot()['entry_reservations'], int(mode != 'expired'))
                    else:
                        connect.assert_not_called()
                        if mode == 'expired':
                            mutex.cancel_entry_turn.assert_called_once()
if __name__=='__main__':unittest.main()
