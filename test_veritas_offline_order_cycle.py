"""OFFLINE ONLY: existing broker-coordinator contract against an in-memory venue.

This is not TBank sandbox or evidence of real fills. No account, token, network
client, production switch or persistent application data is used or modified.
"""
from dataclasses import replace
import math
import unittest
from unittest.mock import patch

import veritas_broker as B
import veritas_execution as E
import veritas_live as L


class OfflineVenue(B.BrokerAdapter):
    def __init__(self):
        self.orders={};self.positions={};self.submissions=0
        self.alive=True;self.reject=False;self.lose_ack=False

    def heartbeat(self):return self.alive
    def list_positions(self):
        return [B.BrokerPosition(a,q) for a,q in self.positions.items()]
    def list_open_orders(self):
        return [replace(o) for o in self.orders.values() if o.status in
                (B.OrderStatus.ACK,B.OrderStatus.PARTIAL)]
    def get_order_by_client_id(self,client_order_id):
        o=self.orders.get(client_order_id)
        return replace(o) if o else None
    def submit_order(self,intent,quantity):
        assert math.isfinite(quantity) and quantity>0
        if intent.client_order_id in self.orders:
            return self.get_order_by_client_id(intent.client_order_id)
        self.submissions+=1
        status=B.OrderStatus.REJECTED if self.reject else B.OrderStatus.ACK
        o=B.BrokerOrder(intent.client_order_id,'offline-'+str(self.submissions),
            intent.asset,intent.side,quantity,0.,None,status)
        self.orders[o.client_order_id]=o
        if self.lose_ack:
            self.lose_ack=False
            raise TimeoutError('offline acknowledgement lost after acceptance')
        return replace(o)
    def cancel_order(self,broker_order_id):
        o=next(o for o in self.orders.values() if o.broker_order_id==broker_order_id)
        if o.status in (B.OrderStatus.ACK,B.OrderStatus.PARTIAL):
            o.status=B.OrderStatus.CANCELLED
        return replace(o)
    def fill(self,client_order_id,quantity,price):
        o=self.orders[client_order_id]
        if o.status not in (B.OrderStatus.ACK,B.OrderStatus.PARTIAL):
            raise ValueError('offline order cannot receive another fill')
        if not 0<quantity<=o.quantity-o.filled_quantity or price<=0:
            raise ValueError('invalid offline fill')
        previous=o.filled_quantity
        o.avg_fill_price=((o.avg_fill_price or 0.)*previous+price*quantity)/(previous+quantity)
        o.filled_quantity+=quantity
        sign=1 if o.side in ('BUY','BUY_TO_COVER') else -1
        self.positions[o.asset]=self.positions.get(o.asset,0.)+sign*quantity
        o.status=B.OrderStatus.FILLED if o.filled_quantity==o.quantity else B.OrderStatus.PARTIAL
        return replace(o)


class OfflineOrderCycleTests(unittest.TestCase):
    def setUp(self):
        self.net=patch('socket.create_connection',side_effect=AssertionError('Network forbidden in offline tests'))
        self.net.start();self.addCleanup(self.net.stop)
        self.venue=OfflineVenue();self.coordinator=B.LiveExecutionCoordinator(self.venue)
        self.intent=E.build_order_intent('Currency','CNYRUBF','LONG',.1,12.75,'1m',
            '2026-10-07T10:00:00Z','OFFLINE_FIXTURE',production_eligible=False)
    def preflight(self,expected=None,eligible=True):
        return self.coordinator.preflight({'eligible':eligible,'blockers':[]},
            self.coordinator.reconcile(expected or {}))
    def submit(self):return self.coordinator.submit_once(self.intent,10,self.preflight())

    def test_ack_full_fill_and_position_reconciliation(self):
        o=self.submit();self.assertEqual(o.status,B.OrderStatus.ACK)
        o=self.venue.fill(o.client_order_id,10,12.76)
        self.assertEqual(o.status,B.OrderStatus.FILLED)
        self.assertEqual(o.avg_fill_price,12.76)
        r=self.coordinator.reconcile({'CNYRUBF':10})
        self.assertTrue(r.ok);self.assertEqual(r.open_orders,0)

    def test_partial_cancel_keeps_executed_quantity_and_cancels_only_remainder(self):
        o=self.submit();self.venue.fill(o.client_order_id,4,12.75)
        o=self.venue.cancel_order(o.broker_order_id)
        self.assertEqual((o.status,o.filled_quantity),(B.OrderStatus.CANCELLED,4))
        self.assertTrue(self.coordinator.reconcile({'CNYRUBF':4}).ok)
        self.assertEqual(self.venue.list_open_orders(),[])
        with self.assertRaises(ValueError):self.venue.fill(o.client_order_id,6,12.75)
        self.assertEqual(self.venue.cancel_order(o.broker_order_id),o)

    def test_two_partial_fills_have_correct_weighted_price(self):
        o=self.submit();self.venue.fill(o.client_order_id,4,12.75)
        o=self.venue.fill(o.client_order_id,6,12.80)
        self.assertAlmostEqual(o.avg_fill_price,12.78)
        self.assertEqual(o.filled_quantity,10)

    def test_same_intent_twice_never_submits_a_second_order(self):
        first=self.submit();second=self.submit()
        self.assertEqual(first,second);self.assertEqual(self.venue.submissions,1)

    def test_lost_ack_recovers_known_order_by_id_without_resubmit(self):
        self.venue.lose_ack=True
        with self.assertRaises(TimeoutError):self.submit()
        recovered=self.submit()
        self.assertEqual(recovered.status,B.OrderStatus.ACK)
        self.assertEqual(self.venue.submissions,1)

    def test_recreated_coordinator_reads_existing_offline_venue_journal(self):
        first=self.submit();self.coordinator=B.LiveExecutionCoordinator(self.venue)
        self.assertEqual(self.submit(),first)
        self.assertEqual(self.venue.submissions,1)

    def test_rejected_order_never_creates_a_position(self):
        self.venue.reject=True;o=self.submit()
        self.assertEqual(o.status,B.OrderStatus.REJECTED)
        self.assertEqual(self.venue.list_positions(),[])
        self.assertEqual(self.venue.list_open_orders(),[])

    def test_position_mismatch_prevents_new_risk(self):
        self.venue.positions['CNYRUBF']=1.
        gate=self.preflight()
        self.assertFalse(gate['eligible'])
        self.assertIn('BROKER_POSITION_MISMATCH',gate['blockers'])
        with self.assertRaises(RuntimeError):self.coordinator.submit_once(self.intent,10,gate)
        self.assertEqual(self.venue.submissions,0)

    def test_lost_heartbeat_and_denied_authorization_prevent_submission(self):
        for alive,eligible in ((False,True),(True,False)):
            with self.subTest(alive=alive,eligible=eligible):
                self.venue.alive=alive
                with self.assertRaises(RuntimeError):
                    self.coordinator.submit_once(self.intent,10,self.preflight(eligible=eligible))
        self.assertEqual(self.venue.submissions,0)

    def test_authorization_defaults_remain_disarmed(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(L._armed(),{'enabled':False,'armed':False,'ready':False})
        self.assertFalse(self.intent.production_eligible)
