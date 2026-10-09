from datetime import datetime, timezone
import unittest

from google.protobuf.timestamp_pb2 import Timestamp

from tinkoff.invest.grpc import orders_pb2
from veritas_tbank_order_stream import (
    CNY_UID, OrderStreamError, TBankOrderEventStream, TARGETS,
)


ACCOUNT = "test-account"
TOKEN = "isolated-token-for-stream-tests"
OTHER_UID = "11111111-1111-1111-1111-111111111111"


class DummyChannel:
    def close(self):
        pass


def ts():
    value = Timestamp()
    value.FromDatetime(datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc))
    return value


class TBankOrderEventStreamTests(unittest.TestCase):
    def stream(self, **changes):
        args = dict(token=TOKEN, account_id=ACCOUNT, instrument_uid=CNY_UID,
                    environment="sandbox", channel=DummyChannel())
        args.update(changes)
        return TBankOrderEventStream(**args)

    def test_targets_are_exact_official_endpoints(self):
        self.assertEqual(TARGETS["production"], "invest-public-api.tbank.ru:443")
        self.assertEqual(TARGETS["sandbox"], "sandbox-invest-public-api.tbank.ru:443")

    def test_invalid_configuration_fails_before_network(self):
        for kwargs in (
            {"token": "", "account_id": ACCOUNT},
            {"token": TOKEN, "account_id": ""},
            {"token": TOKEN, "account_id": ACCOUNT, "environment": "live"},
            {"token": TOKEN, "account_id": ACCOUNT, "on_event": "not-callable"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(OrderStreamError):
                TBankOrderEventStream(channel=DummyChannel(), **kwargs)

    def test_order_state_normalization_and_scope_filter(self):
        stream = self.stream()
        state = orders_pb2.OrderStateStreamResponse.OrderState(
            order_id="exchange-1",
            order_request_id="11111111-2222-3333-4444-555555555555",
            account_id=ACCOUNT,
            instrument_uid=CNY_UID,
            execution_report_status=orders_pb2.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
            direction=orders_pb2.ORDER_DIRECTION_BUY,
            lots_requested=5,
            lots_executed=2,
            lots_left=3,
            lots_cancelled=0,
            created_at=ts(),
        )
        state.order_price.currency = "rub"
        state.order_price.units = 12
        state.order_price.nano = 345000000
        event = stream._order_event(state)
        self.assertEqual(event["kind"], "ORDER_STATE")
        self.assertEqual(event["client_order_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(event["status"], "EXECUTION_REPORT_STATUS_PARTIALLYFILL")
        self.assertEqual(event["direction"], "ORDER_DIRECTION_BUY")
        self.assertEqual(event["lots_executed"], 2)
        self.assertEqual(event["order_price"], "12.345")

        state.instrument_uid = OTHER_UID
        self.assertIsNone(stream._order_event(state))

    def test_trade_normalization_and_scope_filter(self):
        stream = self.stream()
        item = orders_pb2.OrderTrades(
            order_id="exchange-2",
            account_id=ACCOUNT,
            instrument_uid=CNY_UID,
            direction=orders_pb2.ORDER_DIRECTION_SELL,
            created_at=ts(),
        )
        trade = item.trades.add()
        trade.trade_id = "trade-1"
        trade.quantity = 2
        trade.price.units = 12
        trade.price.nano = 600000000
        trade.date_time.CopyFrom(ts())
        event = stream._trade_event(item)
        self.assertEqual(event["kind"], "TRADE")
        self.assertEqual(event["direction"], "ORDER_DIRECTION_SELL")
        self.assertEqual(event["trades"][0]["price"], "12.6")
        self.assertEqual(event["trades"][0]["quantity"], 2)

        item.account_id = "other-account"
        self.assertIsNone(stream._trade_event(item))

    def test_duplicate_stream_events_are_coalesced(self):
        stream = self.stream()
        event = {
            "kind": "ORDER_STATE", "account_id": ACCOUNT, "instrument_uid": CNY_UID,
            "broker_order_id": "x", "received_at": "same",
        }
        fingerprint = stream._fingerprint(event)
        self.assertTrue(stream._remember("ORDER_STATE", fingerprint))
        self.assertFalse(stream._remember("ORDER_STATE", fingerprint))

    def test_status_is_read_only_and_starts_idle(self):
        stream = self.stream()
        status = stream.status()
        self.assertFalse(status["started"])
        self.assertEqual(status["order_state_stream"], "IDLE")
        self.assertEqual(status["trades_stream"], "IDLE")


if __name__ == "__main__":
    unittest.main()
