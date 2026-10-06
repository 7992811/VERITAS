"""Outbox regressions using a real transactional SQLite database as a SQL adapter.

The adapter translates PostgreSQL types/placeholders/locking syntax only. State
changes, uniqueness, transaction rollback, and competing senders use real SQL.
PostgreSQL's SKIP LOCKED behavior additionally needs its deployment integration
gate; these tests do not claim SQLite implements PostgreSQL row locks.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import veritas_currency_notifications as N
import veritas_user_teaching as UT


NOW = datetime(2026, 10, 6, 20, 15, tzinfo=timezone.utc)
REF = "@veritas_test"
CHAT = "-1001234567890"
KEY = "local-test-notification-key-not-a-production-secret"
ENV = {
    "VERITAS_CURRENCY_NOTIFICATIONS_KEY": KEY,
    "VERITAS_CURRENCY_NOTIFICATIONS_CHAT_ID": REF,
    "VERITAS_CURRENCY_NOTIFICATIONS_ENABLED": "1",
    "VERITAS_CURRENCY_NOTIFICATIONS_MAX_AGE_S": "900",
}


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class Connection:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.in_transaction = True
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()
        finally:
            self.in_transaction = False

    def execute(self, sql, params=()):
        if "FOR UPDATE" in sql and not self.in_transaction:
            raise AssertionError("PostgreSQL locks require an explicit transaction under autocommit")
        sql = sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = sql.replace("TIMESTAMPTZ", "TEXT").replace("JSONB", "TEXT")
        sql = sql.replace("::jsonb", "").replace("%s", "?")
        sql = sql.replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP")
        sql = sql.replace("FOR UPDATE SKIP LOCKED", "").replace("FOR UPDATE", "")
        params = tuple(x.isoformat() if isinstance(x, datetime) else x for x in params)
        statements = [x.strip() for x in sql.split(";") if x.strip()]
        if len(statements) > 1:
            if params:
                raise AssertionError("No multi-statement parameters")
            for statement in statements:
                cursor = self.db.execute(statement)
            return Cursor(cursor)
        return Cursor(self.db.execute(sql, params))


def order_row(kind="OPEN", age_s=0, order_id=1, client_id="order-1"):
    exit_kind = kind in ("CLOSE", "REDUCE")
    opened = NOW - timedelta(hours=2)
    price, notional = (12.75, 5100.0) if exit_kind else (12.5, 5000.0)
    return {
        "client_order_id": client_id, "order_id": order_id, "trade_id": f"Currency:CNYRUBF:{client_id}",
        "created_at": NOW - timedelta(seconds=age_s),
        "side": "SELL" if exit_kind else "BUY", "price": price,
        "notional_rub": notional, "fraction_nav": notional / 10000,
        "fee_rub": notional * .0004, "reason": "STOP" if exit_kind else "CANONICAL_ENTRY",
        "payload": {"execution_horizon": "5m", "stop_price": 12.4, "target_price": 12.9,
                    "pwin": .73, "pwin_source": "MODEL_PRIOR_UNCALIBRATED",
                    "price_source_identity": {"primary_source": "MOEX ISS", "contract_id": "CNYRUBF"},
                    "market_observed_at": (NOW - timedelta(seconds=age_s + 4)).isoformat(),
                    "execution_model": {"reference_price": price},
                    "realized_gross_pnl_rub": 100 if exit_kind else None},
        "trade_direction": "LONG", "trade_opened_at": opened,
        "trade_closed_at": NOW if kind == "CLOSE" else None,
        "trade_status": "CLOSED" if kind == "CLOSE" else "OPEN",
        "trade_avg_entry_price": 12.5, "trade_horizon": "5m",
        "gross_pnl_rub": 100.0 if exit_kind else 0.0,
        "trade_fees_rub": 4.04 if exit_kind else 2.0, "funding_rub": 0.0,
        "net_pnl_rub": 95.96 if kind == "CLOSE" else None,
        "trade_payload": {"pwin": .71},
        "remaining_units": 0 if kind == "CLOSE" else 200 if kind == "REDUCE" else 400,
        "position_avg_entry_price": None if kind == "CLOSE" else 12.5,
        "position_stop_price": 12.39, "position_payload": {},
        "initial_nav_rub": 10000, "portfolio_realized_pnl_rub": 100 if exit_kind else 0,
        "portfolio_fees_rub": 4.04 if exit_kind else 2.0,
        "portfolio_funding_rub": 0,
        "first_order_id": order_id if kind == "OPEN" else order_id - 1,
        "entry_notional_rub": 5000,
    }


class OutboxFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "outbox.sqlite")
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        with self.connect() as c, c.transaction():
            N.ensure_schema(c)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def connect(self):
        return Connection(self.path)

    def event(self, kind="OPEN", age_s=0, client_id="order-1"):
        with self.connect() as c, c.transaction():
            count = c.execute(f"SELECT COUNT(*) AS n FROM {N.TABLE}").fetchone()["n"]
            row = order_row(kind, age_s, count + 1, client_id)
            snapshot = N.build_snapshot(row)
            result = c.execute(f"""INSERT INTO {N.TABLE}
                (client_order_id,order_id,trade_id,portfolio_name,asset,kind,occurred_at,
                 destination_ref,snapshot,text_snapshot,status)
                VALUES (%s,%s,%s,'Currency','CNYRUBF',%s,%s,%s,%s,%s,'PENDING')
                RETURNING event_id""", (client_id, row["order_id"], row["trade_id"], kind,
                row["created_at"], REF, json.dumps(snapshot), N.format_message(snapshot)))
            return result.fetchone()["event_id"]

    def call(self, action, body=None, now=NOW, headers=None):
        body = {"chat_id": REF, "telegram_chat_id": CHAT, **(body or {})}
        with patch.object(N, "_now", return_value=now):
            return N.handle_request(N.PREFIX + action, body, headers or {"X-Veritas-Notifications-Key": KEY}, self.connect)

    def claim(self, **kwargs):
        data, code = self.call("claim", **kwargs)
        self.assertEqual(code, 200, data)
        return data["event"]

    def get(self, event_id):
        with self.connect() as c:
            return c.execute(f"SELECT * FROM {N.TABLE} WHERE event_id=%s", (event_id,)).fetchone()

    def begin(self, event, **kwargs):
        return self.call("begin", event, **kwargs)

    def complete(self, event, status, **kwargs):
        return self.call("complete", {**event, "status": status, **kwargs})


class OutboxTests(OutboxFixture):
    def test_order_entry_event_precedes_legacy_setup_and_position_events(self):
        row = order_row("ADD")
        row["payload"].update(entry_event_id="new-add", setup_event_id="legacy-order")
        row["position_payload"]["r66_event_id"] = "original-open"
        row["trade_payload"]["r66_event_id"] = "original-trade"
        self.assertEqual(N.build_snapshot(row)["setup_event_id"], "new-add")
        row["payload"].pop("entry_event_id")
        self.assertEqual(N.build_snapshot(row)["setup_event_id"], "legacy-order")
        row["payload"].pop("setup_event_id")
        self.assertEqual(N.build_snapshot(row)["setup_event_id"], "original-open")

    def test_structural_trace_preserves_epoch_and_historical_policy(self):
        row = order_row()
        signal = NOW.timestamp() - 3600.25
        trace = UT.entry_trace({"event": {"event_id": "structure-1", "signal_at": signal}}, "Currency")
        row["payload"].update(entry_event_id="structure-1", user_teaching_trace=trace)
        # A later runtime version and clock cannot rewrite the entry provenance.
        with patch.object(N.CTC, "VERSION", "LATER_POLICY"), patch.object(N, "_now", side_effect=AssertionError("no clock lookup")):
            snapshot = N.build_snapshot(row)
        self.assertEqual(snapshot["structural_signal_at"], signal)
        self.assertEqual(snapshot["structural_signal_at_utc"], datetime.fromtimestamp(signal, timezone.utc).isoformat())
        self.assertEqual(snapshot["structural_policy_version"], trace["structural_policy_version"])
        self.assertEqual(snapshot["entry_ctc_version"], trace["ctc_version"])
        self.assertEqual(snapshot["user_teaching_trace_sha256"], trace["trace_sha256"])
        self.assertEqual(snapshot["policy_version"], "LATER_POLICY")

    def test_add_never_inherits_original_event_timestamp_or_invalid_trace(self):
        row = order_row("ADD")
        original = UT.entry_trace({"event": {"event_id": "original-open", "signal_at": NOW.timestamp() - 7200}}, "Currency")
        row["payload"]["entry_event_id"] = "new-add"
        row["position_payload"]["user_teaching_trace"] = original
        snapshot = N.build_snapshot(row)
        self.assertIsNone(snapshot["structural_signal_at"])
        self.assertIsNone(snapshot["entry_ctc_version"])
        original["timeframe_entry_context"]["event"]["event_id"] = "new-add"
        row["payload"]["user_teaching_trace"] = original
        self.assertIsNone(N.build_snapshot(row)["structural_signal_at"])

    def test_close_retains_matching_original_trace_without_current_position(self):
        row = order_row("CLOSE")
        signal = NOW.timestamp() - 7200
        trace = UT.entry_trace({"event": {"event_id": "original-open", "signal_at": signal}}, "Currency")
        row["payload"]["setup_event_id"] = "original-open"
        row["trade_payload"]["user_teaching_trace"] = trace
        self.assertEqual(N.build_snapshot(row)["structural_signal_at"], signal)

    def test_missing_invalid_or_millisecond_signal_time_is_never_replaced(self):
        row = order_row()
        row["payload"]["entry_event_id"] = "structure-1"
        for raw in (None, True, "invalid", NOW.timestamp() * 1000):
            with self.subTest(raw=raw):
                row["payload"]["user_teaching_trace"] = UT.entry_trace(
                    {"event": {"event_id": "structure-1", "signal_at": raw}}, "Currency")
                snapshot = N.build_snapshot(row)
                self.assertIsNone(snapshot["structural_signal_at"])
                self.assertIsNone(snapshot["structural_signal_at_utc"])

    def test_real_snapshot_preserves_source_clock_quantity_and_model_label(self):
        row = order_row()
        text = N.format_message(N.build_snapshot(row))
        self.assertIn("23:14:56 МСК", text)
        self.assertIn("Учебный портфель", text)
        self.assertIn("Фильтр потенциала к издержкам: 1.1×", text)
        self.assertIn("Оценка модели: 73.0%", text)
        self.assertNotIn("Вероятность:", text)
        self.assertNotIn("контрактов", text)
        self.assertIn("5 000.00 ₽", text)

    def test_missing_probability_is_omitted_not_invented(self):
        row = order_row()
        row["payload"].pop("pwin")
        row["trade_payload"] = {}
        text = N.format_message(N.build_snapshot(row))
        self.assertNotIn("Оценка модели", text)
        self.assertNotIn("Вероятность", text)

    def test_closed_trade_uses_authoritative_net_and_entry_notional_basis(self):
        s = N.build_snapshot(order_row("CLOSE"))
        self.assertEqual(s["net_pnl_rub"], 95.96)
        self.assertAlmostEqual(s["return_on_entry_notional_pct"], 1.9192)
        text = N.format_message(s)
        self.assertIn("Итоговый доход: +95.96 ₽", text)
        self.assertIn("Доход от цены: +100.00 ₽", text)
        self.assertIn("Фондирование: 0.00 ₽", text)
        self.assertIn("Комиссия: 4.04 ₽", text)
        self.assertIn("Позиция после операции: закрыта", text)

    def test_parallel_workers_claim_only_one_and_preserve_global_order(self):
        first = self.event()
        self.event("CLOSE", client_id="order-2")
        # _now is deterministic for both workers; SQLite serializes the local
        # transactions while the production query has explicit row locking.
        with patch.object(N, "_now", return_value=NOW), ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: N.handle_request(N.PREFIX + "claim",
                {"chat_id": REF, "telegram_chat_id": CHAT},
                {"X-Veritas-Notifications-Key": KEY}, self.connect), range(2)))
        events = [reply[0]["event"] for reply in replies if reply[0].get("event")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], first)

    def test_expired_claim_reclaimed_with_new_token_old_worker_rejected(self):
        self.event()
        old = self.claim()
        new = self.claim(now=NOW + timedelta(seconds=N.CLAIM_LEASE_S + 1))
        self.assertEqual(new["event_id"], old["event_id"])
        self.assertNotEqual(new["claim_token"], old["claim_token"])
        data, code = self.begin(old)
        self.assertEqual(code, 409)
        self.assertEqual(data["error"], "CLAIM_TOKEN_MISMATCH")

    def test_repeated_begin_never_authorizes_second_send(self):
        self.event()
        event = self.claim()
        self.assertEqual(self.begin(event)[1], 200)
        data, code = self.begin(event)
        self.assertEqual(code, 409)
        self.assertEqual(data["error"], "SEND_ALREADY_BEGUN_OR_FINAL")

    def test_sending_timeout_unknown_no_resend_and_exit_can_progress(self):
        first = self.event()
        second = self.event("CLOSE", client_id="close-2")
        event = self.claim()
        self.begin(event)
        later = NOW + timedelta(seconds=N.SEND_LEASE_S + 1)
        next_event = self.claim(now=later)
        self.assertEqual(self.get(first)["status"], "UNKNOWN")
        self.assertEqual(next_event["event_id"], second)
        data, code = self.complete(event, "RETRY", definite_failure=True)
        self.assertEqual(code, 409)
        self.assertEqual(data["error"], "AMBIGUOUS_DELIVERY_REQUIRES_RECONCILIATION")

    def test_success_ack_idempotent_after_restart_and_ambiguity(self):
        event_id = self.event()
        event = self.claim()
        self.begin(event)
        self.call("status", now=NOW + timedelta(seconds=N.SEND_LEASE_S + 1))
        self.assertEqual(self.get(event_id)["status"], "UNKNOWN")
        first, code = self.complete(event, "SENT", message_id=123)
        self.assertEqual((code, first["status"]), (200, "SENT"))
        # Each API call has opened and closed its own DB connection: no memory
        # state survives, exactly as on worker/API restart.
        repeat, code = self.complete(event, "SENT", message_id=123)
        self.assertEqual(code, 200)
        self.assertTrue(repeat["idempotent"])
        self.assertEqual(self.complete(event, "SENT", message_id=124)[1], 409)

    def test_sent_requires_begin_message_id_and_correct_resolved_destination(self):
        self.event()
        event = self.claim()
        self.assertEqual(self.complete(event, "SENT", message_id=123)[1], 409)
        self.begin(event)
        self.assertEqual(self.complete(event, "SENT")[1], 400)
        self.assertEqual(self.complete(event, "SENT", message_id=123, telegram_chat_id="-10099999999")[1], 409)

    def test_only_definite_failure_can_retry_and_rate_limit_is_durable(self):
        self.event()
        event = self.claim()
        self.begin(event)
        data, code = self.complete(event, "RETRY", definite_failure=True, retry_after_s=60, error_code="TELEGRAM_429")
        self.assertEqual((code, data["status"]), (200, "RETRY"))
        self.assertIsNone(self.claim(now=NOW + timedelta(seconds=30)))
        retry = self.claim(now=NOW + timedelta(seconds=61))
        self.assertNotEqual(event["claim_token"], retry["claim_token"])
        self.begin(retry, now=NOW + timedelta(seconds=61))
        data, code = self.complete(retry, "RETRY", error_code="TELEGRAM_ReadTimeout")
        self.assertEqual((code, data["status"]), (200, "UNKNOWN"))

    def test_begin_response_lost_before_telegram_can_release_claim(self):
        self.event()
        event = self.claim()
        # Either CLAIMED (begin never arrived) or SENDING (reply lost) is safe
        # to retry only when the sender attests it did not call Telegram.
        self.assertEqual(self.complete(event, "RETRY", definite_failure=True)[0]["status"], "RETRY")
        event = self.claim(now=NOW + timedelta(seconds=31))
        self.begin(event, now=NOW + timedelta(seconds=31))
        self.assertEqual(self.complete(event, "RETRY", definite_failure=True)[0]["status"], "RETRY")

    def test_stale_entries_expire_but_close_uses_frozen_delayed_snapshot(self):
        first = self.event("OPEN", age_s=1200)
        last = self.event("CLOSE", age_s=1100, client_id="close-2")
        event = self.claim()
        self.assertEqual(self.get(first)["status"], "EXPIRED")
        self.assertEqual(event["event_id"], last)
        self.assertTrue(event["text"].startswith("Уведомление с задержкой"))
        frozen = self.get(last)["text_snapshot"]
        self.assertTrue(event["text"].endswith(frozen))
        self.assertEqual(self.begin(event)[1], 200)

    def test_entry_age_rechecked_at_begin(self):
        event_id = self.event(age_s=890)
        event = self.claim()
        data, code = self.begin(event, now=NOW + timedelta(seconds=11))
        self.assertEqual((code, data["error"]), (409, "ENTRY_SIGNAL_EXPIRED"))
        self.assertEqual(self.get(event_id)["status"], "EXPIRED")

    def test_destination_reference_and_resolved_id_cannot_drift_on_retry(self):
        self.event()
        data, code = self.call("claim", {"chat_id": "@someone_else"})
        self.assertEqual(code, 409)
        event = self.claim()
        self.complete(event, "RETRY", definite_failure=True, retry_after_s=1)
        data, code = self.call("claim", {"telegram_chat_id": "-1009876543210"}, now=NOW + timedelta(seconds=2))
        self.assertEqual((code, data["error"]), (409, "RESOLVED_DESTINATION_CHANGED"))

    def test_auth_rejected_without_any_database_access(self):
        with patch.object(self, "connect", side_effect=AssertionError("must not access DB")):
            self.assertEqual(self.call("claim", headers={"X-Veritas-Notifications-Key": "wrong"})[1], 401)

    def test_status_contains_no_secrets_claims_or_destination(self):
        self.event()
        event = self.claim()
        self.begin(event)
        data, code = self.call("status", now=NOW + timedelta(seconds=N.SEND_LEASE_S + 1))
        self.assertEqual(code, 200)
        self.assertEqual(data["counts"]["UNKNOWN"], 1)
        self.assertTrue(data["unknown_requires_reconciliation"])
        serialized = json.dumps(data)
        for forbidden in (KEY, REF, CHAT, event["claim_token"], "order-1"):
            self.assertNotIn(forbidden, serialized)

    def test_failed_is_terminal_and_does_not_hold_exit(self):
        self.event()
        close_id = self.event("CLOSE", client_id="close-2")
        event = self.claim()
        self.begin(event)
        self.assertEqual(self.complete(event, "FAILED", error_code="TELEGRAM_403")[0]["status"], "FAILED")
        self.assertEqual(self.claim()["event_id"], close_id)

    def test_enqueuing_other_assets_or_portfolios_does_not_query(self):
        class NoQueries:
            def execute(self, *args):
                raise AssertionError("Other portfolios must never reach the outbox")
        for p, a in (("Champion", "CNYRUBF"), ("Currency", "BTC"), ("Currency", "CNYRUBf")):
            self.assertIsNone(N.enqueue_order(NoQueries(), p, a, "id"))


class AccountingAtomicityTests(OutboxFixture):
    """The real enqueue SQL with actual uniqueness/rollback, not mocked inserts."""

    def setUp(self):
        super().setUp()
        self.accounting_row = order_row()
        r = self.accounting_row
        with self.connect() as c, c.transaction():
            c.execute("""
                CREATE TABLE paper_portfolios (name TEXT PRIMARY KEY,
                  initial_nav_rub REAL,realized_pnl_rub REAL,fees_rub REAL,funding_rub REAL);
                CREATE TABLE paper_trades (trade_id TEXT PRIMARY KEY,direction TEXT,
                  opened_at TEXT,closed_at TEXT,status TEXT,avg_entry_price REAL,
                  horizon TEXT,gross_pnl_rub REAL,fees_rub REAL,funding_rub REAL,
                  net_pnl_rub REAL,payload TEXT);
                CREATE TABLE paper_positions (portfolio_name TEXT,asset TEXT,
                  active_trade_id TEXT,units REAL,avg_entry_price REAL,stop_price REAL,payload TEXT);
                CREATE TABLE paper_orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  client_order_id TEXT UNIQUE,trade_id TEXT,portfolio_name TEXT,asset TEXT,
                  created_at TEXT,side TEXT,price REAL,notional_rub REAL,fee_rub REAL,
                  fraction_nav REAL,reason TEXT,payload TEXT);
            """)
            c.execute("INSERT INTO paper_portfolios VALUES ('Currency',10000,0,2,0)")
            c.execute("""INSERT INTO paper_trades VALUES
                (%s,'LONG',%s,NULL,'OPEN',12.5,'5m',0,2,0,NULL,%s)""",
                (r["trade_id"], r["trade_opened_at"], json.dumps(r["trade_payload"])))
            c.execute("""INSERT INTO paper_positions VALUES
                ('Currency','CNYRUBF',%s,400,12.5,12.39,'{}')""", (r["trade_id"],))

    def enqueue(self, c):
        r = self.accounting_row
        c.execute("""INSERT INTO paper_orders
            (client_order_id,trade_id,portfolio_name,asset,created_at,side,price,
             notional_rub,fee_rub,fraction_nav,reason,payload)
            VALUES (%s,%s,'Currency','CNYRUBF',%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(client_order_id) DO NOTHING""",
            (r["client_order_id"], r["trade_id"], r["created_at"], r["side"], r["price"],
             r["notional_rub"], r["fee_rub"], r["fraction_nav"], r["reason"], json.dumps(r["payload"])))
        return N.enqueue_order(c, "Currency", "CNYRUBF", r["client_order_id"])

    def test_enqueue_deduplicates_identical_client_order_after_restart(self):
        with self.connect() as c, c.transaction():
            created = self.enqueue(c)
        with self.connect() as c, c.transaction():
            repeated = self.enqueue(c)
        self.assertEqual(created["status"], "PENDING")
        self.assertIsNone(repeated)
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT COUNT(*) AS n FROM {N.TABLE}").fetchone()["n"], 1)

    def test_accounting_transaction_rollback_removes_event(self):
        with self.assertRaisesRegex(RuntimeError, "accounting failure"):
            with self.connect() as c, c.transaction():
                self.enqueue(c)
                raise RuntimeError("accounting failure")
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT COUNT(*) AS n FROM {N.TABLE}").fetchone()["n"], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()["n"], 0)

    def test_unconfigured_event_never_replayed_after_configuration(self):
        with patch.dict(os.environ, {"VERITAS_CURRENCY_NOTIFICATIONS_KEY": ""}):
            with self.connect() as c, c.transaction():
                event = self.enqueue(c)
        self.assertEqual(event["status"], "SKIPPED_UNCONFIGURED")
        self.assertIsNone(self.claim())
        # Calling enqueue again cannot convert a skipped historical order into
        # a new signal, even though the configuration now exists.
        with self.connect() as c, c.transaction():
            self.assertIsNone(self.enqueue(c))
        self.assertEqual(self.get(event["event_id"])["status"], "SKIPPED_UNCONFIGURED")

    def test_snapshot_does_not_change_when_trade_or_market_facts_change(self):
        with self.connect() as c, c.transaction():
            created = self.enqueue(c)
        self.accounting_row["price"] = 999
        self.accounting_row["payload"]["target_price"] = 1000
        event = self.claim()
        self.assertEqual(created["event_id"], event["event_id"])
        self.assertIn("Цена входа: 12.5000", event["text"])
        self.assertNotIn("999", event["text"])


if __name__ == "__main__":
    unittest.main()
