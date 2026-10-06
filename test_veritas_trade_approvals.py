"""Reconstructed approval tests; unverified until run in isolated CI.

The ordinary suite uses a durable SQLite DBAPI adapter and real transactions,
constraints, restart and thread races. PostgreSQL tests run only when the
explicit VERITAS_TRADING_TEST_DSN names ci_ephemeral_test_only. They never read
DATABASE_URL, Telegram tokens or broker configuration.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
import uuid

from veritas_trade_approvals import (
    ApprovalError, TradeApprovals, canonical_terms, TABLE, SCOPES, CALLBACKS, AUDIT,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
KEY = b"isolated-tests-only-approval-key-32-bytes-long"
OWNER = 123456789
BOT = 987654321


def terms(event="signal-1", action="OPEN", **changes):
    value = {
        "account_id": "test-account", "instrument_uid": "test-instrument",
        "asset": "CNYRUBF", "canonical_event_id": event,
        "action": action, "direction": "LONG", "lots": 3,
        "limit_price": Decimal("11.123400"), "stop_price": Decimal("10.90"),
        "target_price": Decimal("11.70"), "native_timeframe": "1h",
        "signal_at": 1791360000, "event_id": event,
        "policy_version": "ctc-revision-test", "minimum_potential_multiple": Decimal("1.1"),
        "account_snapshot_version": "v1", "positions_version": "flat",
        "atr": Decimal("0.080"), "quote_as_of": "2026-10-07T08:00:00+00:00",
    }
    value.update(changes)
    return value


class SQLiteCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        return self.cursor.rowcount


class SQLiteConnection:
    """Tiny testing adapter; production code and SQL remain PostgreSQL-first."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def execute(self, statement, params=()):
        if "FOR UPDATE" in statement.upper() and not self.db.in_transaction:
            raise AssertionError("Row locks require an explicit transaction")
        statement = re.sub(r"\bBIGSERIAL PRIMARY KEY\b",
                           "INTEGER PRIMARY KEY AUTOINCREMENT", statement)
        statement = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", statement)
        statement = re.sub(r"\bJSONB\b", "TEXT", statement)
        statement = statement.replace("::jsonb", "").replace("%s", "?")
        statement = re.sub(r"\s+FOR UPDATE(?: SKIP LOCKED)?", "", statement)
        values = tuple(v.isoformat() if isinstance(v, datetime) else v for v in params)
        return SQLiteCursor(self.db.execute(statement, values))


class FaultingConnection:
    def __init__(self, inner, predicate):
        self.inner = inner
        self.predicate = predicate

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args):
        return self.inner.__exit__(*args)

    def transaction(self):
        return self.inner.transaction()

    def execute(self, statement, params=()):
        if self.predicate(statement, params):
            raise RuntimeError("injected transaction failure")
        return self.inner.execute(statement, params)


class Helpers:
    def setup_repository(self):
        self.now = NOW
        self.repo = TradeApprovals(self.connect, KEY, clock=lambda: self.now)
        self.repo.ensure_schema()

    def create(self, value=None, **kwargs):
        args = dict(owner_user_id=OWNER, private_chat_id=OWNER, bot_id=BOT)
        args.update(kwargs)
        return self.repo.create(terms() if value is None else value, **args)

    def deliver(self, row, message_id=100):
        current = self.repo.get(row["proposal_id"])
        if current["delivery_token"] is None:
            current = self.repo.claim_delivery(row["proposal_id"], "delivery-test-worker")
        return self.repo.mark_delivered(
            row["proposal_id"], bot_id=row["bot_id"], private_chat_id=row["private_chat_id"],
            message_id=message_id, terms_hash=row["terms_hash"],
            delivery_token=current["delivery_token"],
        )

    def decide(self, row, action="approve", callback_id=None, **changes):
        args = dict(sender_user_id=row["owner_user_id"],
                    private_chat_id=row["private_chat_id"],
                    message_id=row["telegram_message_id"],
                    bot_id=row["bot_id"], callback_query_id=callback_id or uuid.uuid4().hex)
        args.update(changes)
        return self.repo.decide(row["callbacks"][action], **args)

    def approved(self, value=None, message_id=100):
        return self.decide(self.deliver(self.create(value), message_id))

    def claim(self, row, **changes):
        args = dict(terms_hash=row["terms_hash"], worker_id="broker-test-worker",
                    economics_revision=row["economics_revision"])
        args.update(changes)
        return self.repo.claim_approved(row["proposal_id"], **args)

    def submit(self, row, outcome="ACCEPTED", **changes):
        row = self.claim(row) if row["status"] == "APPROVED" else row
        args = dict(client_order_id=row["client_order_id"])
        if outcome == "ACCEPTED":
            args.update(broker_order_id="order-" + row["proposal_id"],
                        broker_status="NEW", filled_lots=0)
        args.update(changes)
        return self.repo.record_submission(row["proposal_id"], row["claim_token"],
                                           outcome=outcome, **args)

    def code(self, expected, function, *args, **kwargs):
        with self.assertRaises(ApprovalError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def count(self, table):
        assert table in {TABLE, SCOPES, CALLBACKS, AUDIT}
        with self.connect() as c:
            return c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def change_raw(self, proposal_id, assignments, values):
        with self.connect() as c:
            with c.transaction():
                c.execute(f"UPDATE {TABLE} SET {assignments} WHERE proposal_id=%s",
                          tuple(values) + (proposal_id,))


class TradeApprovalTests(Helpers, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "approvals.sqlite")
        self.connect = lambda: SQLiteConnection(self.path)
        self.setup_repository()

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_decimal_json_and_direction_side(self):
        value = canonical_terms(terms(limit_price=Decimal("11.123400000000000000000001")))
        self.assertEqual(value["limit_price"], "11.123400000000000000000001")
        self.assertEqual(value["atr"], "0.08")
        self.assertEqual(value["minimum_potential_multiple"], "1.1")
        self.assertEqual(value["side"], "BUY")
        raw = json.dumps(value).replace('"11.123400000000000000000001"',
                                        "11.123400000000000000000001")
        self.assertEqual(canonical_terms(raw), value)
        self.assertEqual(canonical_terms(terms(action="CLOSE"))["side"], "SELL")
        short = terms(direction="SHORT", stop_price="11.70", target_price="10.90")
        self.assertEqual(canonical_terms(short)["side"], "SELL")
        short.update(action="REDUCE")
        self.assertEqual(canonical_terms(short)["side"], "BUY")
        self.assertNotIn("side", terms())

    def test_strict_json_rejects_float_duplicate_and_nonfinite(self):
        self.code("FLOAT_NOT_ALLOWED", canonical_terms, terms(extra={"nested": [1.1]}))
        self.code("DUPLICATE_JSON_KEY", canonical_terms, '{"lots":1,"lots":2}')
        self.code("NONFINITE_JSON_NUMBER", canonical_terms, '{"lots":NaN}')
        self.code("NONFINITE_OR_EXCESSIVE_DECIMAL", canonical_terms,
                  terms(atr=Decimal("Infinity")))
        self.code("STRICT_JSON_TERMS_REQUIRED", canonical_terms, terms(date=NOW))

    def test_integer_lots_entry_geometry_and_exit_can_omit_geometry(self):
        for value in (True, 0, -1, "3", Decimal("3")):
            self.code("POSITIVE_INTEGER_LOTS_REQUIRED", canonical_terms, terms(lots=value))
        self.code("INVALID_ENTRY_GEOMETRY", canonical_terms, terms(stop_price="11.5"))
        self.code("DIRECTION_SIDE_MISMATCH", canonical_terms, terms(side="SELL"))
        self.code("LIMIT_ORDER_REQUIRED", canonical_terms, terms(order_type="MARKET"))
        exit_terms = terms(action="CLOSE", stop_price=None, target_price=None)
        self.assertEqual(canonical_terms(exit_terms)["action"], "CLOSE")

    def test_constructor_and_identity_fail_without_database(self):
        def forbidden():
            raise AssertionError("database must not be accessed")
        self.code("SIGNING_KEY_TOO_SHORT", TradeApprovals, forbidden, b"short")
        for values in ({"owner_user_id": 0}, {"private_chat_id": -100},
                       {"private_chat_id": OWNER + 1}, {"bot_id": True}):
            with self.assertRaises(ApprovalError):
                self.create(**values)
        self.assertEqual(self.count(TABLE), 0)

    def test_short_expiry_required(self):
        for expiry in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(seconds=301)):
            self.code("INVALID_PROPOSAL_EXPIRY", self.create, expires_at=expiry)
        self.code("TIMEZONE_AWARE_TIMESTAMP_REQUIRED", self.create,
                  expires_at=NOW.replace(tzinfo=None))
        self.assertEqual(self.count(TABLE), 0)

    def test_duplicate_creation_does_not_extend_ttl_or_change_callbacks(self):
        first = self.create()
        self.now += timedelta(seconds=30)
        again = self.create()
        self.assertTrue(again["idempotent"])
        for key in ("proposal_id", "client_order_id", "expires_at", "terms_hash", "callbacks"):
            self.assertEqual(first[key], again[key])
        self.assertEqual(self.count(TABLE), 1)
        self.code("EVENT_TERMS_CONFLICT", self.create, terms(lots=4))
        self.code("EVENT_TERMS_CONFLICT", self.create, economics_revision=2)
        self.code("EVENT_TERMS_CONFLICT", self.create,
                  owner_user_id=OWNER + 1, private_chat_id=OWNER + 1)

    def test_event_uniqueness_includes_account_and_action(self):
        first = self.create()
        close = self.create(terms(action="CLOSE"))
        other = self.create(terms(account_id="another-test-account"))
        self.assertEqual(len({first["proposal_id"], close["proposal_id"], other["proposal_id"]}), 3)

    def test_terms_hash_does_not_authorise_tampered_terms(self):
        row = self.create()
        altered = canonical_terms(terms(lots=8))
        self.change_raw(row["proposal_id"], "terms_json=%s::jsonb",
                        (json.dumps(altered),))
        self.code("TERMS_INTEGRITY_FAILED", self.repo.get, row["proposal_id"])
        self.code("TERMS_INTEGRITY_FAILED", self.repo.claim_delivery,
                  row["proposal_id"], "worker")

    def test_immutable_recipient_and_expiry_envelope_is_signed(self):
        for index, (column, value) in enumerate((
            ("expires_at", NOW + timedelta(hours=1)),
            ("owner_user_id", OWNER + 1),
            ("bot_id", BOT + 1),
            ("economics_revision", 2),
        )):
            row = self.create(terms(event=f"immutable-{index}"))
            if column == "owner_user_id":
                self.change_raw(row["proposal_id"], "owner_user_id=%s,private_chat_id=%s",
                                (value, value))
            else:
                self.change_raw(row["proposal_id"], column + "=%s", (value,))
            self.code("TERMS_INTEGRITY_FAILED", self.repo.get, row["proposal_id"])

    def test_callbacks_are_short_and_do_not_expose_signing_material(self):
        row = self.create()
        self.assertLess(len(row["callbacks"]["approve"].encode("ascii")), 64)
        self.assertTrue(row["callbacks"]["approve"].startswith("ta:a:"))
        self.assertNotIn("terms_signature", row)
        self.assertNotIn("callback_nonce", row)
        self.assertNotIn(KEY.decode(), json.dumps(row))
        self.assertNotEqual(row["callbacks"]["approve"][-22:], row["callbacks"]["reject"][-22:])

    def test_delivery_is_send_once_across_restart_and_parallel_claims(self):
        row = self.create()
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: self.repo.claim_delivery(row["proposal_id"], "w"), range(6)))
        self.assertEqual(sum(x is not None for x in claims), 1)
        fresh = TradeApprovals(self.connect, KEY, clock=lambda: self.now)
        self.assertIsNone(fresh.claim_delivery(row["proposal_id"], "restarted"))
        self.assertEqual(fresh.list_pending(), [])
        self.assertEqual(fresh.get(row["proposal_id"])["status"], "DELIVERY_SENDING")

    def test_callback_without_exact_successful_delivery_ack_is_denied(self):
        row = self.create()
        row["telegram_message_id"] = 101
        self.code("CALLBACK_OWNER_MESSAGE_BINDING_MISMATCH", self.decide, row)
        claimed = self.repo.claim_delivery(row["proposal_id"], "worker")
        claimed["telegram_message_id"] = 101
        self.code("CALLBACK_OWNER_MESSAGE_BINDING_MISMATCH", self.decide, claimed)
        self.code("DELIVERY_TOKEN_MISMATCH", self.repo.mark_delivered,
                  row["proposal_id"], bot_id=BOT, private_chat_id=OWNER,
                  message_id=101, terms_hash=row["terms_hash"])

    def test_delivery_unknown_is_not_retried_but_late_exact_ack_is_accepted(self):
        row = self.create(expires_at=NOW + timedelta(seconds=300))
        claimed = self.repo.claim_delivery(row["proposal_id"], "worker")
        self.now += timedelta(seconds=61)
        self.repo.expire()
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "DELIVERY_UNKNOWN")
        self.assertEqual(self.repo.list_pending(), [])
        self.assertIsNone(self.repo.claim_delivery(row["proposal_id"], "restarted"))
        delivered = self.deliver(claimed, message_id=234)
        self.assertEqual(delivered["status"], "AWAITING_OWNER")
        self.assertTrue(self.deliver(claimed, message_id=234)["idempotent"])
        self.code("DELIVERY_MESSAGE_ALREADY_BOUND", self.deliver, claimed, message_id=235)
        approved = self.decide(delivered)
        sending = self.claim(approved)
        late_failure = self.repo.record_delivery_unknown(row["proposal_id"], claimed["delivery_token"])
        self.assertEqual(late_failure["status"], sending["status"])

    def test_expired_delivery_is_never_accepted(self):
        row = self.create()
        claimed = self.repo.claim_delivery(row["proposal_id"], "worker")
        self.repo.record_delivery_unknown(row["proposal_id"], claimed["delivery_token"])
        self.now += timedelta(seconds=121)
        self.repo.expire()
        result = self.deliver(claimed)
        self.assertEqual(result["status"], "EXPIRED")
        self.assertFalse(result["accepted"])
        self.assertEqual(self.repo.list_pending(), [])

    def test_delivery_identity_and_token_are_exact(self):
        row = self.create()
        claimed = self.repo.claim_delivery(row["proposal_id"], "worker")
        args = dict(bot_id=BOT, private_chat_id=OWNER, message_id=10,
                    terms_hash=row["terms_hash"], delivery_token=claimed["delivery_token"])
        for field, value in (("bot_id", BOT + 1), ("private_chat_id", OWNER + 1),
                             ("terms_hash", "0" * 64), ("delivery_token", "wrong")):
            bad = dict(args, **{field: value})
            with self.assertRaises(ApprovalError):
                self.repo.mark_delivered(row["proposal_id"], **bad)
        self.assertIsNone(self.repo.get(row["proposal_id"])["telegram_message_id"])

    def test_callback_exact_owner_chat_message_bot_and_hmac(self):
        row = self.deliver(self.create())
        for field, value in (("sender_user_id", OWNER + 1), ("private_chat_id", OWNER + 1),
                             ("message_id", 101), ("bot_id", BOT + 1)):
            self.code("CALLBACK_OWNER_MESSAGE_BINDING_MISMATCH", self.decide, row,
                      **{field: value})
        fake = dict(row, callbacks=dict(row["callbacks"]))
        token = fake["callbacks"]["approve"]
        fake["callbacks"]["approve"] = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.code("CALLBACK_SIGNATURE_INVALID", self.decide, fake)
        self.code("INVALID_TRADE_CALLBACK", self.repo.decide, "pub:news",
                  sender_user_id=OWNER, private_chat_id=OWNER, message_id=100,
                  bot_id=BOT, callback_query_id="wrong-namespace")
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")

    def test_owner_decision_is_durable_and_exact_query_replay_is_idempotent(self):
        row = self.deliver(self.create())
        first = self.decide(row, callback_id="query-1")
        self.repo = TradeApprovals(self.connect, KEY, clock=lambda: self.now)
        again = self.decide(row, callback_id="query-1")
        self.assertEqual(first["status"], "APPROVED")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["decision_status"], "APPROVED")
        self.assertEqual(self.count(CALLBACKS), 1)
        self.assertEqual(len(self.repo.list_approved()), 1)
        self.assertIsNone(again["claim_token"])
        self.code("CALLBACK_REPLAY_CONFLICT", self.decide, row,
                  action="reject", callback_id="query-1")
        self.assertFalse(self.decide(row, callback_id="new-query")["accepted"])

    def test_reject_has_no_execution_and_replay_cannot_flip(self):
        row = self.deliver(self.create())
        rejected = self.decide(row, action="reject", callback_id="reject")
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertTrue(self.decide(row, action="reject", callback_id="reject")["accepted"])
        self.assertFalse(self.decide(row)["accepted"])
        self.assertIsNone(self.claim(rejected))
        self.assertEqual(self.repo.list_unsettled(), [])

    def test_concurrent_create_and_duplicate_click_are_single_decision(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.create(), range(8)))
        self.assertEqual(len({x["proposal_id"] for x in rows}), 1)
        delivered = self.deliver(rows[0])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.decide(delivered), range(8)))
        self.assertEqual(sum(x["accepted"] for x in results), 1)
        self.assertEqual(self.count(CALLBACKS), 1)

    def test_parallel_proposals_serialize_by_account_instrument(self):
        first = self.deliver(self.create(terms(event="first")), message_id=101)
        second = self.deliver(self.create(terms(event="second")), message_id=102)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.decide, (first, second)))
        self.assertEqual(sum(x["accepted"] for x in results), 1)
        blocked = next(x for x in results if not x["accepted"])
        self.assertEqual(blocked["reason_code"], "INSTRUMENT_HAS_UNSETTLED_EXECUTION")
        self.assertEqual(len(self.repo.list_approved()), 1)

    def test_owner_approval_and_audit_failure_roll_back_together(self):
        row = self.deliver(self.create())
        connect = self.connect
        failing = TradeApprovals(
            lambda: FaultingConnection(connect(), lambda sql, p:
                                       f"INSERT INTO {AUDIT}" in sql and "OWNER_APPROVED" in p),
            KEY, clock=lambda: self.now)
        old_repo, self.repo = self.repo, failing
        try:
            with self.assertRaises(RuntimeError):
                self.decide(row)
        finally:
            self.repo = old_repo
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")
        self.assertEqual(self.count(CALLBACKS), 0)
        self.assertEqual(self.decide(row)["status"], "APPROVED")

    def test_unsigned_or_tampered_approval_cannot_be_claimed(self):
        row = self.deliver(self.create())
        self.change_raw(row["proposal_id"], "status=%s,approved_by=%s,approved_at=%s",
                        ("APPROVED", OWNER, NOW))
        self.code("SIGNED_OWNER_APPROVAL_REQUIRED", self.claim, row)
        self.change_raw(row["proposal_id"], "status=%s,approved_by=%s,approved_at=%s",
                        ("AWAITING_OWNER", None, None))
        approved = self.decide(row)
        with self.connect() as c:
            with c.transaction():
                c.execute(f"UPDATE {CALLBACKS} SET decision_signature=%s WHERE proposal_id=%s",
                          ("0" * 64, row["proposal_id"]))
        self.code("CALLBACK_LEDGER_INTEGRITY_FAILED", self.claim, approved)

    def test_economics_revision_and_hash_checked_before_claim(self):
        row = self.approved()
        self.code("APPROVED_ECONOMICS_MISMATCH", self.claim, row, terms_hash="0" * 64)
        self.code("APPROVED_ECONOMICS_MISMATCH", self.claim, row, economics_revision=2)
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "APPROVED")
        self.assertEqual(self.claim(row)["status"], "SENDING")

    def test_claim_is_committed_once_and_timeout_never_requeues(self):
        row = self.approved()
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: self.claim(row), range(6)))
        self.assertEqual(sum(x is not None for x in claims), 1)
        claimed = next(x for x in claims if x)
        self.repo = TradeApprovals(self.connect, KEY, clock=lambda: self.now)
        self.assertIsNone(self.claim(row))
        self.now += timedelta(seconds=121)
        self.repo.expire()
        unknown = self.repo.get(row["proposal_id"])
        self.assertEqual(unknown["status"], "UNKNOWN")
        self.assertIsNone(unknown["filled_lots"])
        self.assertIsNone(self.claim(row))
        self.assertEqual(self.create()["proposal_id"], row["proposal_id"])
        other = self.deliver(self.create(terms(event="later-event")), message_id=101)
        self.assertFalse(self.decide(other)["accepted"])
        accepted = self.submit(claimed)
        self.assertEqual(accepted["status"], "ACKNOWLEDGED")
        self.assertEqual(accepted["client_order_id"], row["client_order_id"])

    def test_expiry_and_revalidation_block_only_pre_submission(self):
        row = self.approved()
        self.now += timedelta(seconds=121)
        self.assertIsNone(self.claim(row))
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "EXPIRED")
        self.now = NOW
        other = self.approved(terms(event="blocked"), message_id=102)
        self.assertEqual(self.repo.block(other["proposal_id"], "PRICE_CHANGED")["status"], "BLOCKED")
        self.assertIsNone(self.claim(other))
        sending = self.claim(self.approved(terms(event="sending"), message_id=103))
        self.code("SUBMISSION_CANNOT_BE_REQUEUED_OR_BLOCKED", self.repo.block,
                  sending["proposal_id"], "CANNOT_RETRY")
        unknown = self.submit(sending, outcome="UNKNOWN")
        self.code("TERMINAL_EXECUTION_REQUIRED", self.repo.mark_execution_reconciled,
                  unknown["proposal_id"], None, 0)

    def test_unknown_first_broker_binding_requires_exact_client_request(self):
        claimed = self.claim(self.approved())
        unknown = self.submit(claimed, outcome="UNKNOWN")
        args = dict(broker_order_id="actual-order", broker_status="NEW", filled_lots=0)
        self.code("CLIENT_ORDER_ID_REQUIRED_FOR_FIRST_BIND", self.repo.update_execution,
                  unknown["proposal_id"], **args)
        self.code("CLIENT_ORDER_ID_MISMATCH", self.repo.update_execution,
                  unknown["proposal_id"], client_order_id=str(uuid.uuid4()), **args)
        result = self.repo.update_execution(unknown["proposal_id"],
                                           client_order_id=unknown["client_order_id"], **args)
        self.assertEqual(result["status"], "ACKNOWLEDGED")
        self.code("BROKER_ORDER_ID_MISMATCH", self.repo.update_execution,
                  unknown["proposal_id"], broker_order_id="wrong-order",
                  broker_status="NEW", filled_lots=0)

    def test_submission_claim_token_and_fill_counts_are_strict(self):
        row = self.claim(self.approved())
        self.code("CLAIM_TOKEN_MISMATCH", self.repo.record_submission,
                  row["proposal_id"], "wrong", outcome="UNKNOWN")
        for filled in (True, -1, 4, "1"):
            self.code("INVALID_EXECUTED_LOTS", self.submit, row,
                      broker_status="PARTIALLY_FILLED", filled_lots=filled)
        self.code("BROKER_STATUS_FILL_MISMATCH", self.submit, row,
                  broker_status="FILLED", filled_lots=2)
        self.code("UNKNOWN_FILL_COUNT_MUST_BE_NULL", self.submit, row,
                  outcome="UNKNOWN", filled_lots=0)
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "SENDING")

    def test_filled_stays_unsettled_until_actual_accounting_then_frees_scope(self):
        filled = self.submit(self.approved(), broker_status="FILLED", filled_lots=3,
                             average_fill_price=Decimal("11.12"))
        self.assertEqual(filled["average_fill_price"], "11.12")
        self.assertFalse(filled["execution_reconciled"])
        self.assertEqual(len(self.repo.list_unsettled()), 1)
        other = self.deliver(self.create(terms(event="after-fill")), message_id=102)
        self.assertFalse(self.decide(other)["accepted"])
        self.code("EXECUTION_RECONCILIATION_MISMATCH", self.repo.mark_execution_reconciled,
                  filled["proposal_id"], filled["broker_order_id"], 2)
        reconciled = self.repo.mark_execution_reconciled(
            filled["proposal_id"], filled["broker_order_id"], 3)
        self.assertTrue(reconciled["execution_reconciled"])
        self.assertEqual(self.repo.list_unsettled(), [])
        self.assertTrue(self.decide(other)["accepted"])

    def test_partial_cancel_and_late_fill_growth_reset_accounting(self):
        row = self.submit(self.approved(), broker_status="PARTIALLY_FILLED", filled_lots=1,
                          average_fill_price="11.10")
        cancelled = self.repo.update_execution(row["proposal_id"],
            broker_order_id=row["broker_order_id"], broker_status="CANCELLED", filled_lots=1,
            average_fill_price="11.10")
        self.repo.mark_execution_reconciled(row["proposal_id"], row["broker_order_id"], 1)
        same = self.repo.update_execution(row["proposal_id"],
            broker_order_id=row["broker_order_id"], broker_status="CANCELLED", filled_lots=1,
            average_fill_price="11.10")
        self.assertTrue(same["execution_reconciled"])
        self.now += timedelta(seconds=1)
        grown = self.repo.update_execution(row["proposal_id"],
            broker_order_id=row["broker_order_id"], broker_status="CANCELLED", filled_lots=2,
            average_fill_price="11.11")
        self.assertFalse(grown["execution_reconciled"])
        self.assertEqual(grown["filled_lots"], 2)
        self.assertEqual(len(self.repo.list_unsettled()), 1)
        self.assertEqual(cancelled["status"], "CANCELLED")

    def test_broker_observations_never_regress_or_invent_fills(self):
        row = self.submit(self.approved(), broker_status="PARTIALLY_FILLED", filled_lots=2,
                          average_fill_price="11.12")
        unknown = self.submit(row, outcome="UNKNOWN")
        self.assertTrue(unknown["ignored"])
        self.assertEqual(unknown["filled_lots"], 2)
        lower = self.repo.update_execution(row["proposal_id"],
            broker_order_id=row["broker_order_id"], broker_status="PARTIALLY_FILLED", filled_lots=1)
        self.assertTrue(lower["ignored"])
        old = self.repo.update_execution(row["proposal_id"],
            broker_order_id=row["broker_order_id"], broker_status="FILLED", filled_lots=3,
            observed_at=NOW - timedelta(seconds=1))
        self.assertTrue(old["ignored"])
        self.code("FUTURE_BROKER_OBSERVATION", self.repo.update_execution, row["proposal_id"],
            broker_order_id=row["broker_order_id"], broker_status="FILLED", filled_lots=3,
            observed_at=NOW + timedelta(seconds=10))

    def test_definitive_rejection_without_order_is_reconcilable(self):
        rejected = self.submit(self.approved(), outcome="REJECTED")
        self.assertEqual(rejected["filled_lots"], 0)
        self.assertEqual(rejected["status"], "BROKER_REJECTED")
        self.assertIsNone(rejected["broker_order_id"])
        self.assertEqual(len(self.repo.list_unsettled()), 1)
        self.repo.mark_execution_reconciled(rejected["proposal_id"], None, 0)
        self.assertEqual(self.repo.list_unsettled(), [])

    def test_terminal_filter_precedes_limit_and_account_filter_is_exact(self):
        for index in range(3):
            row = self.submit(self.approved(terms(event=f"old-{index}"), message_id=100+index),
                              outcome="REJECTED")
            self.repo.mark_execution_reconciled(row["proposal_id"], None, 0)
        current = self.submit(self.approved(terms(event="current"), message_id=200),
                              outcome="UNKNOWN")
        result = self.repo.list_unsettled(account_id="test-account", limit=1)
        self.assertEqual([x["proposal_id"] for x in result], [current["proposal_id"]])
        self.assertEqual(self.repo.list_unsettled(account_id="other-account"), [])
        self.assertEqual(self.repo.list_pending(owner_user_id=OWNER + 1), [])


@unittest.skipUnless(os.environ.get("VERITAS_TRADING_TEST_DSN"),
                     "explicit isolated PostgreSQL test DSN is not configured")
class PostgresTradeApprovalTests(Helpers, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Never fall back to DATABASE_URL or any application connection helper.
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
            dsn = os.environ["VERITAS_TRADING_TEST_DSN"]
            parsed = conninfo_to_dict(dsn)
        except Exception:
            raise RuntimeError("Unable to parse isolated PostgreSQL test configuration") from None
        if parsed.get("dbname") != "ci_ephemeral_test_only":
            raise RuntimeError("VERITAS_TRADING_TEST_DSN must name ci_ephemeral_test_only")
        cls.driver, cls.dict_row, cls.dsn = psycopg, dict_row, dsn
        cls.schema = "trade_approval_test_" + uuid.uuid4().hex
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("CREATE SCHEMA " + cls.schema)
        except Exception:
            raise RuntimeError("Unable to create isolated PostgreSQL test schema") from None

    @classmethod
    def tearDownClass(cls):
        with cls.driver.connect(cls.dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def connect(self):
        return self.driver.connect(
            self.dsn, autocommit=True, row_factory=self.dict_row,
            options=f"-c search_path={self.schema} -c statement_timeout=5000 -c lock_timeout=1500",
        )

    def setUp(self):
        self.setup_repository()
        with self.connect() as c:
            with c.transaction():
                for table in (CALLBACKS, AUDIT, TABLE, SCOPES):
                    c.execute("DELETE FROM " + table)

    test_parallel_creation_and_duplicate_callback = (
        TradeApprovalTests.test_concurrent_create_and_duplicate_click_are_single_decision)
    test_parallel_approval_instrument_scope = (
        TradeApprovalTests.test_parallel_proposals_serialize_by_account_instrument)
    test_owner_decision_transaction_rollback = (
        TradeApprovalTests.test_owner_approval_and_audit_failure_roll_back_together)
    test_terminal_accounting_keeps_instrument_scope = (
        TradeApprovalTests.test_filled_stays_unsettled_until_actual_accounting_then_frees_scope)

    def test_skip_locked_proposal_returns_without_waiting(self):
        row = self.approved()
        with self.connect() as held:
            with held.transaction():
                held.execute(f"SELECT proposal_id FROM {TABLE} WHERE proposal_id=%s FOR UPDATE",
                             (row["proposal_id"],)).fetchone()
                start = time.monotonic()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(self.claim, row).result(timeout=3)
                self.assertIsNone(result)
                self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.claim(row)["status"], "SENDING")

    def test_skip_locked_scope_returns_without_waiting(self):
        row = self.approved()
        with self.connect() as held:
            with held.transaction():
                held.execute(f"""SELECT account_id FROM {SCOPES}
                    WHERE account_id=%s AND instrument_uid=%s FOR UPDATE""",
                    (row["account_id"], row["instrument_uid"])).fetchone()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertIsNone(pool.submit(self.claim, row).result(timeout=3))
        self.assertEqual(self.claim(row)["status"], "SENDING")

    def test_database_unique_scope_cannot_be_bypassed_by_another_coordinator(self):
        self.approved(terms(event="first"), message_id=101)
        second = self.create(terms(event="second"))
        with self.assertRaises(self.driver.errors.UniqueViolation):
            with self.connect() as c:
                with c.transaction():
                    c.execute(f"UPDATE {TABLE} SET status='APPROVED' WHERE proposal_id=%s",
                              (second["proposal_id"],))
        self.assertEqual(self.repo.get(second["proposal_id"])["status"], "PENDING_DELIVERY")


if __name__ == "__main__":
    unittest.main()
