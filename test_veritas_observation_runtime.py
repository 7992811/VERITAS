"""Actual canonical accounting with isolated table state; no providers or loops."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import veritas_observation_path as PATH
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_position_guard as G
import veritas_price_source as SOURCE
import veritas_trade_diagnostics as DIAG
from test_veritas_timeframe_policy import structural_row


class Result:
    def __init__(self, row=None): self.row = deepcopy(row)
    def fetchone(self): return self.row


class AccountingTables:
    """Table/JSON merge and savepoint semantics needed by the real writers."""
    def __init__(self):
        self.position = self.trade = None
        self.orders = []
        self.fees = self.gross = 0.
        self.aborted = self.fail_metadata = self.fail_next_read = False
        self.rollbacks = 0

    @contextmanager
    def transaction(self):
        before = deepcopy((self.position, self.trade, self.orders, self.fees, self.gross))
        try:
            yield self
        except Exception:
            self.position, self.trade, self.orders, self.fees, self.gross = before
            self.aborted = False
            self.rollbacks += 1
            raise

    def execute(self, sql, args=()):
        q = ' '.join(sql.split())
        if self.aborted:
            raise RuntimeError('transaction is aborted')
        if self.fail_next_read and q.startswith('SELECT * FROM paper_positions'):
            self.fail_next_read = False
            self.aborted = True
            raise RuntimeError('optional post-fill read failed')
        if self.fail_metadata and q.startswith('UPDATE paper_trades SET payload=COALESCE'):
            if set(json.loads(args[0])) == {'observation_path'}:
                self.fail_metadata = False
                self.aborted = True
                raise RuntimeError('optional observation write failed')
        if q.startswith('SELECT * FROM paper_positions'):
            return Result(self.position)
        if q.startswith('SELECT payload FROM paper_trades'):
            return Result({'payload':self.trade['payload']} if self.trade else None)
        if q.startswith('SELECT * FROM paper_trades'):
            return Result(self.trade)
        if q.startswith('SELECT 1 AS ok FROM paper_trades'):
            return Result()
        if q.startswith('SELECT 1 AS ok FROM paper_orders'):
            found = any(o['client_order_id'] == args[0] for o in self.orders) if len(args)==1 else any(
                (o['portfolio_name'],o['asset'],o['side'],o['payload'].get('entry_event_id')) == args
                for o in self.orders)
            return Result({'ok':1} if found else None)
        if q.startswith('INSERT INTO '):
            table = q.split('INSERT INTO ',1)[1].split('(',1)[0]
            fields = q.split('(',1)[1].split(')',1)[0].split(',')
            row = dict(zip(fields,args))
            row['payload'] = json.loads(row['payload'])
            if table == 'paper_trades':
                self.trade = dict(gross_pnl_rub=0.,funding_rub=0.,net_pnl_rub=None,**row)
            elif table == 'paper_positions': self.position = row
            elif table == 'paper_orders': self.orders.append(row)
            else: raise AssertionError(q)
        elif q.startswith('UPDATE paper_portfolios SET realized_pnl_rub='):
            self.gross += args[0]; self.fees += args[1]
        elif q.startswith('UPDATE paper_portfolios SET fees_rub='):
            self.fees += args[0]
        elif q.startswith('UPDATE paper_trades SET gross_pnl_rub='):
            self.trade['gross_pnl_rub'] += args[0]; self.trade['fees_rub'] += args[1]
        elif q.startswith('UPDATE paper_trades SET fees_rub='):
            self.trade['fees_rub'] += args[0]
            self.trade['max_fraction'] = max(self.trade['max_fraction'],args[1])
            self.trade['payload'].update(json.loads(args[2]))
        elif q.startswith('UPDATE paper_trades SET closed_at='):
            keys = ('closed_at','avg_exit_price','net_pnl_rub','return_on_entry_nav',
                    'profitable','meaningful_win','status')
            self.trade.update(zip(keys,args))
            self.trade['payload'].update(json.loads(args[7]))
        elif q.startswith(('UPDATE paper_trades SET payload=','UPDATE paper_positions SET payload=')):
            row = self.trade if q.startswith('UPDATE paper_trades') else self.position
            if row is not None:
                value = json.loads(args[0])
                if '||' in q: row['payload'].update(value)
                else: row['payload'] = value
        elif q.startswith('UPDATE paper_positions SET units='):
            if 'avg_entry_price=' in q:
                self.position.update(zip(('units','avg_entry_price','last_price','target_fraction','updated_at'),args))
                self.position['payload'] = json.loads(args[5])
            else:
                self.position.update(zip(('units','last_price','target_fraction','updated_at'),args))
                self.position['payload'].update(json.loads(args[4]))
        elif q.startswith('DELETE FROM paper_positions'): self.position = None
        else: raise AssertionError('Unexpected actual accounting query: '+q)
        return Result()


class CanonicalObservationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026,10,7,9,tzinfo=timezone.utc)
        self.tables = AccountingTables()
        self.row = structural_row(self.clock,timeframe='5m')
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(G._quotes,{},clear=True))
        self.stack.enter_context(patch.dict(G._source_quotes,{},clear=True))
        self.stack.enter_context(patch.object(P.VCN,'enqueue_order'))

    def open(self, row=None, fraction=.1, clock=None):
        row = self.row if row is None else row
        return R.canonical_open_or_add(self.tables,{'high_water_nav_rub':10000.},
            'Aggressive','NQ','LONG',row['price'],fraction,10000.,clock or self.clock,row,'TEST')

    def quote(self, at, price):
        return {'asset':'NQ','price':price,'source_names':{'primary':'ProFinance NASD100_FUT'},
                'source_gate_pass':True,'market_open':True,'observed_at':at.isoformat()}

    def close(self, *, partial=False, clock=None, quote=None):
        clock = clock or self.clock+timedelta(seconds=30)
        quote = quote or self.quote(clock,self.tables.position['stop_price'])
        G.publish_quote('NQ',quote)
        return R.canonical_close_or_reduce(self.tables,{},'Aggressive',deepcopy(self.tables.position),
            quote['price'],.05 if partial else 0.,10000.,clock,'RISK_REDUCTION' if partial else 'STOP')

    def test_successful_none_return_records_actual_immutable_entry_and_valid_stop_loss(self):
        self.assertIsNone(self.open())  # The real writer does not return a fee.
        entry = deepcopy(self.tables.trade['payload'])
        event = entry['entry_event_snapshot']
        first = entry['observation_path']
        self.assertTrue(first['started_at_entry'])
        self.assertEqual(first['observation_count'],1)
        self.assertEqual(first['original_entry_price'],self.tables.trade['avg_entry_price'])
        self.assertNotEqual(first['original_entry_price'],self.row['price'])  # Actual adverse fill.
        self.assertEqual(first['initial_stop_price'],event['stop_price'])
        self.assertEqual(first['entry_atr'],event['atr'])
        self.assertEqual(first['source_identity'],entry['entry_execution_source_identity'])
        self.assertEqual(entry['data_integrity_status'],'OK')
        self.assertGreater(self.close(),0.)
        trade = self.tables.trade
        self.assertIsNone(self.tables.position)
        self.assertEqual(trade['payload']['exit_reason'],'STOP')
        self.assertEqual(trade['payload']['close_reason'],'STOP')
        self.assertEqual(trade['payload']['entry_execution_model'],entry['entry_execution_model'])
        diagnosed = DIAG.diagnose(trade)
        self.assertTrue(diagnosed['learning_eligible'],diagnosed)
        self.assertEqual(diagnosed['primary_attribution'],'VALID_STRUCTURAL_STOP_LOSS')
        self.assertAlmostEqual(trade['net_pnl_rub'],self.tables.gross-self.tables.fees)

    def test_failed_last_metadata_write_does_not_cancel_exit_or_leave_clean_evidence(self):
        for partial in (False,True):
            with self.subTest(partial=partial):
                self.tables = AccountingTables()
                self.open()
                initial_fee = self.tables.fees
                self.tables.fail_metadata = True
                fee = self.close(partial=partial)
                self.assertGreater(fee,0.)
                self.assertEqual(self.tables.rollbacks,1)
                self.assertFalse(self.tables.aborted)
                self.assertAlmostEqual(self.tables.fees,initial_fee+fee)
                witness = self.tables.trade['payload']['observation_path']
                self.assertEqual(witness['coverage_status'],'INCOMPLETE')
                self.assertGreater(witness['invalid_observation_count'],0)
                self.assertFalse(PATH.assessment(self.tables.trade)['eligible'])
                if partial:
                    self.assertGreater(self.tables.position['units'],0.)
                    self.assertEqual(self.tables.position['payload']['observation_path'],witness)
                    self.assertNotIn('exit_reason',self.tables.position['payload'])
                    self.assertEqual(self.tables.position['payload']['last_reduce_reason'],'RISK_REDUCTION')
                else:
                    self.assertIsNone(self.tables.position)
                    self.assertEqual(self.tables.trade['payload']['exit_reason'],'STOP')

    def test_cache_change_between_observation_and_accounting_cannot_replace_selected_quote(self):
        self.open()
        at = self.clock+timedelta(seconds=30)
        selected = self.quote(at,self.tables.position['stop_price'])
        later = self.quote(at+timedelta(seconds=1),selected['price']-2.)
        for q in (selected,later):
            q.update(best_bid=q['price']-.01,best_ask=q['price']+.01)
        original = PATH.record
        def record_then_refresh(*args,**kwargs):
            result = original(*args,**kwargs)
            G.publish_quote('NQ',later)
            return result
        with patch.object(PATH,'record',side_effect=record_then_refresh):
            self.close(clock=at+timedelta(seconds=1),quote=selected)
        order = self.tables.orders[-1]
        payload = self.tables.trade['payload']
        self.assertEqual(order['payload']['reference_price'],selected['price'])
        self.assertEqual(order['payload']['market_observed_at'],selected['observed_at'])
        self.assertEqual(payload['observation_path']['last_price'],selected['price'])
        self.assertEqual(payload['observation_path']['last_observed_at'],selected['observed_at'])
        self.assertEqual(payload['last_exit_market_observed_at'],selected['observed_at'])
        self.assertEqual(payload['last_exit_execution_model']['reference_price'],selected['price'])
        self.assertEqual(payload['last_exit_execution_model']['bid'],selected['best_bid'])
        self.assertEqual(payload['last_exit_execution_model']['ask'],selected['best_ask'])
        self.assertEqual(order['payload']['price_source_identity'],SOURCE.identity('NQ',selected))

    def test_entry_fill_uses_book_from_selected_quote_instead_of_stale_signal_row(self):
        quote = self.quote(self.clock,self.row['price'])
        quote.update(best_bid=quote['price']-.01,best_ask=quote['price']+.01)
        row = dict(self.row,best_bid=99.,best_ask=103.,_execution_quote=quote)
        self.open(row)
        payload = self.tables.trade['payload']
        fill = payload['entry_execution_model']
        self.assertEqual(fill['bid'],quote['best_bid'])
        self.assertEqual(fill['ask'],quote['best_ask'])
        self.assertEqual(fill['reference_price'],quote['price'])
        self.assertAlmostEqual(fill['fill_price'],quote['best_ask']*(1.+P.VX.VC.SLIPPAGE_RATE))
        self.assertEqual(payload['entry_execution_observed_at'],quote['observed_at'])
        self.assertEqual(payload['observation_path']['original_entry_price'],fill['fill_price'])

    def test_gate_and_fill_share_selected_alias_book_or_no_book_without_stale_fallback(self):
        for has_book in (True,False):
            with self.subTest(has_book=has_book):
                self.tables = AccountingTables()
                quote = self.quote(self.clock,self.row['price'])
                if has_book:
                    quote.update(bid=quote['price']-.01,ask=quote['price']+.01)
                row = dict(self.row,best_bid=99.,best_ask=103.,_execution_quote=quote)
                self.open(row)
                self.assertIsNotNone(self.tables.trade)
                payload = self.tables.trade['payload']
                fill = payload['entry_execution_model']
                self.assertEqual(fill['bid'],quote.get('bid'))
                self.assertEqual(fill['ask'],quote.get('ask'))
                self.assertEqual(payload['fill_economics_gate']['modeled_entry_fill'],fill['fill_price'])

    def test_frozen_quote_still_requires_source_and_time_checks(self):
        self.open()
        at = self.clock+timedelta(seconds=30)
        valid = self.quote(at,100.)
        G.publish_quote('NQ',valid)
        for bad in (dict(valid,observed_at=(at-timedelta(hours=1)).isoformat()),
                    dict(valid,source_names={'primary':'ANOTHER_SOURCE'}),
                    dict(valid,source_gate_pass=False)):
            frozen = dict(self.tables.position,_execution_quote=bad,_execution_quote_frozen=True)
            self.assertEqual(G.quote_for_position(frozen,now=at),{})

    def test_actual_new_entry_preserves_non_ok_integrity_without_ui_patch(self):
        row = dict(self.row,data_integrity_status='UNKNOWN')
        with patch.object(P,'_v90j_entry_patch',return_value={}):
            self.open(row)
        self.assertEqual(self.tables.trade['payload']['data_integrity_status'],'UNKNOWN')
        self.assertEqual(self.tables.position['payload']['data_integrity_status'],'UNKNOWN')

    def test_stale_execution_audit_or_truthy_return_cannot_invent_a_fill(self):
        row = dict(self.row,_execution_audit={'status':'EXECUTED'})
        with patch.object(P,'CANONICAL_ACCOUNTING_OPEN_OR_ADD',return_value=.4), \
             patch.object(PATH,'record') as record:
            self.assertEqual(self.open(row),.4)
        record.assert_not_called()
        self.assertEqual(row['_execution_audit']['status'],'NOT_EXECUTED')
        self.assertIsNone(self.tables.position)

    def test_optional_entry_read_failure_keeps_booked_fill_without_fabricated_witness(self):
        original = P.CANONICAL_ACCOUNTING_OPEN_OR_ADD
        def accounting_then_failed_read(*args,**kwargs):
            result = original(*args,**kwargs)
            self.tables.fail_next_read = True
            return result
        with patch.object(P,'CANONICAL_ACCOUNTING_OPEN_OR_ADD',side_effect=accounting_then_failed_read):
            self.assertIsNone(self.open())
        self.assertGreater(self.tables.fees,0.)
        self.assertEqual(len(self.tables.orders),1)
        self.assertEqual(self.tables.rollbacks,1)
        self.assertNotIn('observation_path',self.tables.trade['payload'])

    def test_add_keeps_first_fill_stop_atr_and_does_not_invent_missing_prefix(self):
        self.open()
        before = deepcopy(self.tables.trade['payload'])
        for row in (self.tables.trade,self.tables.position):
            row['payload'].pop('observation_path')
        clock = self.clock+timedelta(minutes=5)
        row = structural_row(clock,timeframe='5m',price=self.row['price']+.2)
        self.open(row,fraction=.15,clock=clock)
        self.assertEqual(len(self.tables.orders),2)
        payload = self.tables.trade['payload']
        for field in ('entry_execution_model','initial_stop_price','entry_atr','entry_event_snapshot'):
            self.assertEqual(payload[field],before[field])
        witness = payload['observation_path']
        self.assertFalse(witness['started_at_entry'])
        self.assertEqual(witness['original_entry_price'],before['entry_execution_model']['fill_price'])
        self.assertEqual(witness['coverage_status'],'INCOMPLETE')


if __name__ == '__main__': unittest.main()
