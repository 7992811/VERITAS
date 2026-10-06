"""Native MA evidence survives bounded SQL projection without accounting rewrites."""
from copy import deepcopy
from contextlib import contextmanager
import json
import os
import unittest
import uuid

import veritas_learning_integrity as LI
import veritas_ma_rebound as MA
import veritas_trade_audit as AUDIT
from test_veritas_ma_learning_evidence import closed_ma_trade

DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class NativeMAProjectionSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver, self.row_factory = psycopg, dict_row
        self.schema = "ma_learning_projection_"+uuid.uuid4().hex
        with psycopg.connect(DSN) as c:
            if c.execute("SELECT current_database()").fetchone()[0] != "veritas_quality_test":
                raise RuntimeError("Refusing writes outside veritas_quality_test")
            c.execute(f"CREATE SCHEMA {self.schema}")
            c.execute(f"SET search_path TO {self.schema}")
            c.execute("""CREATE TABLE paper_trades(
              trade_id text PRIMARY KEY,asset text,direction text,horizon text,status text,
              opened_at timestamptz,closed_at timestamptz,gross_pnl_rub float8,
              fees_rub float8,funding_rub float8,net_pnl_rub float8,payload jsonb)""")
            c.execute("""CREATE TABLE v90_learning_episodes(
              trade_id text PRIMARY KEY,asset text,direction text,horizon text,
              primary_attribution text,attributions jsonb,learning_action text,
              learning_eligible boolean,payload jsonb)""")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def tearDown(self):
        with self.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def fixture(self, key, period=50, short=False):
        trade = closed_ma_trade(period=period, short=short)
        trade["trade_id"] = key
        # The runtime stores adverse excursion as min(0, previous, signed return).
        trade["payload"]["mae_pct"] = -.3
        trade["payload"].update(strategy_epoch="ORIGINAL_MA_EPOCH",
                                strategy_entry_sha="immutable-ma-entry")
        return trade

    def add_trade(self, trade):
        with self.connect() as c:
            c.execute("INSERT INTO paper_trades ("+",".join(LI.TRADE_FIELDS)+
                      ",payload) VALUES ("+",".join(["%s"]*len(LI.TRADE_FIELDS))+",%s::jsonb)",
                      tuple(trade[k] for k in LI.TRADE_FIELDS)+(json.dumps(trade["payload"]),))
            c.execute("""INSERT INTO v90_learning_episodes VALUES(
              %s,%s,%s,%s,'GOOD_EXECUTION','["GOOD_EXECUTION"]','RETAIN_RULE',TRUE,
              '{"original_note":"preserve"}')""",
              tuple(trade[k] for k in ("trade_id","asset","direction","horizon")))

    def financials(self):
        with self.connect() as c:
            return [dict(row) for row in
                    c.execute("SELECT * FROM paper_trades ORDER BY trade_id").fetchall()]

    def projected(self, c, key):
        return dict(c.execute("SELECT "+LI.trade_projection_sql("t")+","+
            LI.evidence_hash_sql("t")+" AS evidence_hash FROM paper_trades t WHERE trade_id=%s",
            (key,)).fetchone())

    def test_native_ma50_ma200_both_directions_keep_causal_proof_and_financials(self):
        for period in (50,200):
            for short in (False,True):
                key = str(period)+("_short" if short else "_long")
                trade = self.fixture(key, period, short)
                event = trade["payload"]["entry_event_snapshot"]
                proof = event["ma_proof"]
                self.assertTrue(MA.validate_event(event,trade["payload"]["price_source_lock"])["eligible"])
                self.assertIsNone(LI.trade_exclusion(trade))
                # Bulky diagnostics must never enter the learning projection.
                proof["daily_bars"] = [{"close":999999}]*1000
                proof["daily_provenance"]["bars"] = [{"close":999999}]*1000
                proof["approach_bars"][0]["diagnostic_bars"] = [{"close":999999}]*1000
                self.add_trade(trade)
                with self.connect() as c:
                    projected = self.projected(c,key)
                self.assertIsNone(LI.trade_exclusion(projected), projected)
                saved = projected["payload"]["entry_event_snapshot"]
                self.assertEqual(saved["event_id"],event["event_id"])
                self.assertEqual(saved["ma_proof"]["daily_provenance"]["sha256"],
                                 proof["daily_provenance"]["sha256"])
                self.assertEqual(saved["ma_proof"]["daily_provenance"]["native_timeframe"],"1d")
                self.assertEqual(len(saved["ma_proof"]["approach_bars"]),3)
                self.assertNotIn("daily_bars",saved["ma_proof"])
                self.assertNotIn("bars",saved["ma_proof"]["daily_provenance"])
                self.assertNotIn("diagnostic_bars",saved["ma_proof"]["approach_bars"][0])
                self.assertLess(len(json.dumps(projected["payload"])),12000)
        before = self.financials()
        with self.connect() as c:
            result = LI.revalidate_eligible(c)
            readable = c.execute("SELECT count(*) AS n FROM v90_learning_episodes WHERE "+
                                 LI.readable_sql()).fetchone()["n"]
        self.assertEqual(result["verified"],4)
        self.assertEqual(result["excluded"],0)
        self.assertEqual(readable,4)
        self.assertEqual(self.financials(),before)

    def test_absent_and_null_ma_proof_do_not_reclassify_structural_events(self):
        for explicit_null in (False,True):
            trade = self.fixture("structural_"+str(explicit_null))
            p = trade["payload"]; event = p["entry_event_snapshot"]
            event.update(event_id="STF_"+str(explicit_null),
                         event_type="SAME_TIMEFRAME_STRUCTURAL_BREAKOUT")
            event.pop("ma_rebound_version")
            event.pop("ma_proof")
            if explicit_null:
                event["ma_proof"] = None
            p.update(idea_event_id=event["event_id"],r66_event_id=event["event_id"])
            self.assertIsNone(LI.trade_exclusion(trade))
            self.add_trade(trade)
            with self.connect() as c:
                projected = self.projected(c,trade["trade_id"])
            self.assertIsNone(projected["payload"]["entry_event_snapshot"]["ma_proof"])
            self.assertIsNone(LI.trade_exclusion(projected))
        before = self.financials()
        with self.connect() as c:
            self.assertEqual(LI.revalidate_eligible(c)["verified"],2)
        self.assertEqual(self.financials(),before)

    def test_changed_daily_proof_invalidates_hash_and_malformed_or_oversize_proof_stays_excluded(self):
        trade = self.fixture("changed")
        self.add_trade(trade)
        with self.connect() as c:
            self.assertEqual(LI.revalidate_eligible(c)["verified"],1)
            original_hash = self.projected(c,"changed")["evidence_hash"]
            trade["payload"]["entry_event_snapshot"]["ma_proof"]["daily_provenance"]["sha256"] = "corrected-daily-digest"
            c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='changed'",
                      (json.dumps(trade["payload"]),))
            self.assertNotEqual(self.projected(c,"changed")["evidence_hash"],original_hash)
            self.assertFalse(c.execute("SELECT "+LI.readable_sql()+
                " AS ok FROM v90_learning_episodes WHERE trade_id='changed'").fetchone()["ok"])

        changes = (
            ("future",lambda e:e["ma_proof"].update(daily_known_at=e["signal_at"])),
            ("native",lambda e:e["ma_proof"]["daily_provenance"].update(native_timeframe="1h")),
            ("source",lambda e:e["ma_proof"]["daily_provenance"]["source_identity"].update(key="FOREIGN:NQ")),
            ("sample",lambda e:e["ma_proof"]["period_evidence"].update(sample_count=49)),
            ("policy",lambda e:e["ma_proof"]["policy"].update(rearm_bars=4)),
            ("missing",lambda e:e.pop("ma_proof")),
            ("scalar",lambda e:e.update(ma_proof="UNOBSERVED")),
            ("proofarray",lambda e:e.update(ma_proof=[{"bars":[1]*1000}]*10)),
            ("arrayscalar",lambda e:e["ma_proof"].update(daily_atr=[1]*1000)),
            ("bararray",lambda e:e["ma_proof"]["approach_bars"].__setitem__(0,[{"close":1}]*1000)),
            ("array",lambda e:e["ma_proof"].update(approach_bars="UNOBSERVED")),
            ("extra",lambda e:e["ma_proof"]["approach_bars"].append({"ts":0})),
            ("oversize",lambda e:e["ma_proof"].update(
                approach_bars=e["ma_proof"]["approach_bars"]+[{"ts":0}]*101)),
        )
        for key, change in changes:
            with self.subTest(case=key):
                invalid = self.fixture(key)
                change(invalid["payload"]["entry_event_snapshot"])
                self.assertEqual(LI.trade_exclusion(invalid),"UNVERIFIED_EVENT")
                self.add_trade(invalid)
                with self.connect() as c:
                    projected = self.projected(c,key)
                self.assertEqual(LI.trade_exclusion(projected),"UNVERIFIED_EVENT")
                self.assertLess(len(json.dumps(projected["payload"])),12000)
                if key=="oversize":
                    self.assertIsNone(projected["payload"]["entry_event_snapshot"]["ma_proof"]["approach_bars"])
        before = self.financials()  # Includes the deliberate proof correction above.
        with self.connect() as c:
            result = LI.revalidate_eligible(c)
        self.assertEqual(result["excluded"],len(changes))
        self.assertEqual(self.financials(),before)

    def test_real_sql_failure_invalidates_process_memory_generation_without_financial_writes(self):
        self.add_trade(self.fixture("preserve"))
        before = self.financials()
        with self.connect() as c:
            c.execute("DROP TABLE v90_learning_episodes")
        prior_generation = LI.generation()
        with self.assertRaises(self.driver.errors.UndefinedTable):
            with self.connect() as c:
                LI.revalidate_eligible(c)
        self.assertGreater(LI.generation(),prior_generation)
        self.assertEqual(self.financials(),before)


if __name__ == "__main__":
    unittest.main()
