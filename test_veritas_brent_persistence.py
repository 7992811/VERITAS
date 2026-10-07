"""Brent provider pin survives real paper accounting and bounded read models.

Fixtures use causal structural geometry and an observed-provider-shaped quote.
Only storage and notification delivery are isolated; live network is forbidden.
The optional SQL projection executes a read-only expression on the configured
isolated test database and never reads or writes a production table.
"""
import copy
import io
import json
import os
import socket
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

import veritas_portfolio as VP
import veritas_portfolio_read_model as VPRM
import veritas_portfolio_runtime as VPR
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_trade_journal_read_model as VTJ
from test_veritas_execution_snapshot import EntryAccountingDB
from test_veritas_timeframe_policy import valid_row


def provider_quote(clock):
    return {
        "asset": "BRENT", "price": 100., "observed_at": clock.isoformat(),
        "source": "ProFinance", "source_names": {"primary": "ProFinance"},
        "source_gate_pass": True, "market_open": True, "direct_sources": 1,
        "raw_label": "Brent oil", "raw_ticker": "brent", "instrument_id": "27",
        "provider_ticker_verified": True, "provider_series_verified": True,
        "source_pin_version": "PROFINANCE_BRENT_PIN_V1", "price_field": "LP",
        "exact_contract_verified": False,
    }


def display_position(clock):
    quote = provider_quote(clock)
    identity = VPS.identity("BRENT", quote)
    position = {
        "asset": "BRENT", "direction": "LONG", "avg_entry_price": 99.,
        "last_price": 100., "price_source_lock": copy.deepcopy(identity),
        "payload": {
            "price_source_lock": copy.deepcopy(identity),
            "entry_execution_source_identity": copy.deepcopy(identity),
            "last_exit_source_identity": copy.deepcopy(identity),
            "source_locked_mark": {"identity": copy.deepcopy(identity),
                                   "price": 100., "observed_at": clock.isoformat()},
            "entry_decision_snapshot": {"unused_proof": [0] * 100},
        },
    }
    basis = VPS.valuation_basis(position, quote)
    position["valuation_basis"] = copy.deepcopy(basis)
    position["payload"]["entry_valuation_basis"] = copy.deepcopy(basis)
    return position


class BrentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime.now(timezone.utc).replace(microsecond=0)
        self.nav = 1_000_000.
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(VPG._quotes, {}, clear=True))
        self.stack.enter_context(patch.dict(VPG._source_quotes, {}, clear=True))
        self.stack.enter_context(patch.object(VPG, "_entry_namespace", None))
        self.stack.enter_context(patch.object(VP.VCN, "enqueue_order"))
        self.stack.enter_context(patch.object(socket, "create_connection",
            side_effect=AssertionError("Network forbidden in Brent accounting test")))
        self.stack.enter_context(patch.object(socket.socket, "connect",
            side_effect=AssertionError("Network forbidden in Brent accounting test")))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def row(self):
        row = valid_row("BRENT", "1h", "LONG", price=100.,
                        source="ProFinance", now=self.clock)
        quote = provider_quote(self.clock)
        identity = VPS.identity("BRENT", quote)
        # The fixture's real causal event already owns the same price series.
        # Supply the provider evidence native ProFinance history now carries.
        context = row["timeframe_entry_context"]
        context["source_identity"] = copy.deepcopy(identity)
        context["event"]["source_identity"] = copy.deepcopy(identity)
        row.update(**quote, _execution_quote=copy.deepcopy(quote),
                   _pwin=.8, _pwin_source="CTC_V2_CANONICAL", _execution_audit={})
        return row

    def assert_pin(self, identity):
        self.assertEqual(identity["key"], "PROFINANCE:Brent oil")
        self.assertIsNone(identity["contract_id"])
        self.assertEqual(identity["source_pin_version"], "PROFINANCE_BRENT_PIN_V1")
        self.assertEqual(identity["provider_ticker"], "brent")
        self.assertEqual(identity["provider_instrument_id"], "27")

    def test_core_rejects_wrong_or_unverified_feed_before_flip_and_any_sql(self):
        changes = (
            {"instrument_id": None},
            {"raw_ticker": "wti"},
            {"instrument_id": "28"},
            {"source": "Yahoo Brent BZ=F", "source_names": {"primary": "Yahoo Brent BZ=F"}},
        )
        for bad_fields in changes:
            for held_direction in (None, "SHORT"):
                with self.subTest(fields=bad_fields, held_direction=held_direction):
                    row = self.row()
                    row["_execution_quote"].update(bad_fields)
                    row["_flip_confirmed"] = True
                    position = display_position(self.clock) if held_direction else None
                    if position:
                        position["direction"] = held_direction
                    db = EntryAccountingDB(position)
                    original = copy.deepcopy(db.position)
                    with patch.object(VP, "_close_or_reduce") as close:
                        result = VP._v90j_base_open_or_add(
                            db, {}, "Champion", "BRENT", "LONG", 100., .1,
                            self.nav, self.clock, row, "BRENT_PIN_REJECTION")
                    self.assertEqual(result, 0.)
                    self.assertEqual(row["_execution_audit"]["reason"], "SOURCE_IDENTITY_MISSING")
                    close.assert_not_called()
                    self.assertEqual(db.queries, [])
                    self.assertEqual(db.orders, [])
                    self.assertEqual(db.position, original)

    def test_canonical_open_persists_provider_pin_in_all_three_ledgers(self):
        row = self.row()
        original_context = copy.deepcopy(row["timeframe_entry_context"])
        db = EntryAccountingDB()
        VPR.canonical_open_or_add(
            db, {"high_water_nav_rub": self.nav}, "Champion", "BRENT", "LONG",
            100., .1, self.nav, self.clock, row, "BRENT_PIN_PERSISTENCE")
        self.assertEqual(len(db.orders), 1, row.get("_execution_audit"))
        self.assertIsNotNone(db.position)
        self.assertIsNotNone(db.inserted_trade)
        order = db.orders[0]["payload"]
        self.assert_pin(order["price_source_identity"])
        self.assert_pin(order["execution_snapshot"]["source_identity"])
        self.assertEqual(order["execution_snapshot"]["quote"]["raw_ticker"], "brent")
        self.assertEqual(order["execution_snapshot"]["quote"]["instrument_id"], "27")
        for stored in (db.position["payload"], db.trade_payload):
            with self.subTest(ledger="position" if stored is db.position["payload"] else "trade"):
                self.assert_pin(stored["price_source_lock"])
                self.assert_pin(stored["entry_execution_source_identity"])
                self.assert_pin(stored["source_locked_mark"]["identity"])
                self.assert_pin(stored["execution_snapshot"]["source_identity"])
                self.assertEqual(stored["entry_valuation_basis"]["source_pin_status"], "PINNED_PROVIDER_FEED")
                self.assertEqual(stored["entry_valuation_basis"]["source_pin_version"], "PROFINANCE_BRENT_PIN_V1")
                self.assertEqual(stored["entry_valuation_basis"]["price_field"], "LP")
                self.assertFalse(stored["entry_valuation_basis"]["exact_contract_verified"])
        self.assertEqual(row["timeframe_entry_context"], original_context)

    def test_display_projection_keeps_pin_marks_and_valuation_without_relabelling_legacy(self):
        position = display_position(self.clock)
        legacy = copy.deepcopy(position)
        for value in (legacy["price_source_lock"], legacy["payload"]["price_source_lock"],
                      legacy["payload"]["entry_execution_source_identity"],
                      legacy["payload"]["last_exit_source_identity"],
                      legacy["payload"]["source_locked_mark"]["identity"]):
            for key in VPS.BRENT_PIN_FIELDS:
                value.pop(key, None)
        for key in ("source_pin_version", "source_pin_status", "provider_series_verified"):
            legacy["valuation_basis"].pop(key, None)
            legacy["payload"]["entry_valuation_basis"].pop(key, None)
        report = {"positions_complete": False, "snapshot_stale": True,
                  "portfolios": [{"name": "Champion", "positions": [position, legacy]}]}
        original = copy.deepcopy(report)
        projected = VPRM.display_report(report)
        shown, old = projected["portfolios"][0]["positions"]
        for key in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity"):
            self.assertEqual(shown["payload"][key], position["payload"][key])
            self.assert_pin(shown["payload"][key])
            self.assertNotIn("source_pin_version", old["payload"][key])
        for key in ("source_locked_mark", "entry_valuation_basis"):
            self.assertEqual(shown["payload"][key], position["payload"][key])
            self.assertEqual(old["payload"][key], legacy["payload"][key])
        self.assertEqual(shown["valuation_basis"], position["valuation_basis"])
        self.assertNotIn("entry_decision_snapshot", shown["payload"])
        self.assertFalse(projected["positions_complete"])
        self.assertTrue(projected["snapshot_stale"])
        self.assertEqual(report, original)


@unittest.skipUnless(os.getenv("VERITAS_QUALITY_TEST_DSN"), "isolated PostgreSQL test database not configured")
class BrentJournalProjectionSQLTests(unittest.TestCase):
    def test_sql_projection_preserves_pin_and_legacy_absence(self):
        import psycopg
        payload = display_position(datetime(2026, 10, 7, 17, 25, tzinfo=timezone.utc))["payload"]
        legacy = copy.deepcopy(payload)
        for key in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity"):
            for pin_key in VPS.BRENT_PIN_FIELDS:
                legacy[key].pop(pin_key, None)
        for pin_key in VPS.BRENT_PIN_FIELDS:
            legacy["source_locked_mark"]["identity"].pop(pin_key, None)
        for key in ("source_pin_version", "source_pin_status", "provider_series_verified"):
            legacy["entry_valuation_basis"].pop(key, None)
        with psycopg.connect(os.environ["VERITAS_QUALITY_TEST_DSN"]) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            for original in (payload, legacy):
                with self.subTest(pinned=original is payload):
                    sql = "SELECT " + VTJ.JOURNAL_PAYLOAD_SQL + " FROM (VALUES (%s::jsonb)) AS sample(payload)"
                    shown = connection.execute(sql, (json.dumps(original),)).fetchone()[0]
                    for key in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity",
                                "source_locked_mark", "entry_valuation_basis"):
                        self.assertEqual(shown[key], original[key])


if __name__ == "__main__":
    unittest.main()
