"""Persisted-label regressions against an explicitly isolated PostgreSQL schema."""
import copy
import json
import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_learning_integrity as LI
from test_veritas_strategy_quality_sql import observed_evidence

DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")

@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class PersistedLearningSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver, self.row_factory = psycopg, dict_row
        self.schema = "learning_integrity_test_"+uuid.uuid4().hex
        self.saved29, self.saved33 = copy.deepcopy(R._v90r29_cache), copy.deepcopy(R._v90r33_cache)
        self.saved_state = copy.deepcopy(R._v90r44_sanitize_state)
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
              trade_id text PRIMARY KEY,asset text,direction text,horizon text,closed_at timestamptz,
              setup_family text,regime text,net_pnl_rub float8,movement_realization_ratio float8,
              capture_ratio float8,primary_attribution text,attributions jsonb,
              learning_action text,learning_eligible boolean,payload jsonb)""")
        for key, kind, net in (("a_clean", "clean", 30), ("b_gold", "proxy", -3336),
                               ("c_unknown", "unknown", -120), ("d_synthetic", "synthetic", -80),
                               ("z_manual", "manual", -50)):
            self.add_trade(key, kind, net)

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def tearDown(self):
        for target, saved in ((R._v90r29_cache, self.saved29), (R._v90r33_cache, self.saved33),
                              (R._v90r44_sanitize_state, self.saved_state)):
            target.clear(); target.update(saved)
        with self.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def add_trade(self, key, kind="clean", net=30):
        opened = datetime(2026,10,6,19,0,tzinfo=timezone.utc)
        asset = "GOLD" if kind=="proxy" else "ETH"
        p = observed_evidence(asset, "LONG", "STF_"+key, opened, "1h")
        p.update(mfe_pct=1.5, mae_pct=-.8, strategy_epoch="IMMUTABLE_OLD_EPOCH",
                 strategy_entry_sha="original-entry-sha", telemetry_completeness=1.0)
        if kind=="proxy":
            for field in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity"):
                p[field].update(key="PROXY:GLD", primary_source="PROXY BRIDGE")
            p["entry_event_snapshot"]["source_identity"] = copy.deepcopy(p["price_source_lock"])
        elif kind=="unknown":
            p.pop("data_integrity_status")
        elif kind=="synthetic":
            p["r66_event_id"] = "R79_SIG_"+key
            p["entry_event_snapshot"]["event_id"] = p["r66_event_id"]
            p["idea_id_verified"] = True
        with self.connect() as c:
            c.execute("""INSERT INTO paper_trades VALUES
              (%s,%s,'LONG','1h','CLOSED',%s,%s,%s,8,2,%s,%s::jsonb)""",
              (key,asset,opened,opened+timedelta(hours=1),net+10,net,json.dumps(p)))
            c.execute("""INSERT INTO v90_learning_episodes VALUES
              (%s,%s,'LONG','1h',%s,'BREAKOUT','TREND',%s,1.0,.7,
               'GOOD_EXECUTION','["GOOD_EXECUTION"]','RETAIN_RULE',%s,%s::jsonb)""",
              (key,asset,opened+timedelta(hours=1),net,kind!="manual",
               json.dumps({"independent_episode_key":key,"original_note":"preserve"})))

    def financials(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM paper_trades ORDER BY trade_id").fetchall()]

    def episodes(self):
        with self.connect() as c:
            return {r["trade_id"]:dict(r) for r in
                    c.execute("SELECT * FROM v90_learning_episodes ORDER BY trade_id").fetchall()}

    def sanitize(self):
        with patch.object(R, "_v90r29_ensure"):
            return R._v90r44_sanitize_learning(self.connect, force=True)

    def test_saved_proxy_unknown_and_synthetic_labels_are_revalidated_without_financial_edits(self):
        before = self.financials()
        result = self.sanitize()
        self.assertIsNone(result["last_error"], result)
        rows = self.episodes()
        self.assertTrue(rows["a_clean"]["learning_eligible"])
        for key in ("b_gold","c_unknown","d_synthetic","z_manual"):
            self.assertFalse(rows[key]["learning_eligible"], key)
        self.assertEqual(rows["b_gold"]["payload"]["learning_integrity"]["exclusion_reason"], "PROXY_PRICE")
        self.assertEqual(rows["c_unknown"]["payload"]["learning_integrity"]["exclusion_reason"], "SOURCE_UNVERIFIED")
        self.assertEqual(rows["d_synthetic"]["payload"]["learning_integrity"]["exclusion_reason"], "UNVERIFIED_EVENT")
        self.assertEqual(rows["a_clean"]["primary_attribution"], "GOOD_EXECUTION")
        self.assertEqual(rows["a_clean"]["payload"]["original_note"], "preserve")
        self.assertNotIn("learning_integrity", rows["z_manual"]["payload"])
        self.assertEqual(self.financials(), before)
        again = self.sanitize()
        self.assertIsNone(again["last_error"], again)
        self.assertEqual(self.episodes(), rows)
        self.assertEqual(self.financials(), before)

    def test_old_flags_cannot_feed_r29_or_r33_before_first_sanitizer(self):
        R._v90r29_cache.update(at=0,profiles={},summary={})
        R._v90r33_cache.update(at=0,n=0)
        with patch.object(P,"_v90r29_ensure"), patch.object(P,"_v90r29_backfill",return_value=0), \
             patch.object(P,"_v90r60_sanitize_duplicate_learning",return_value={}):
            r29 = P._v90r29_refresh(self.connect,force=True)
            r33 = P._v90r33_refresh(self.connect,force=True)
        self.assertEqual(r29["summary"]["eligible_episodes"],0)
        self.assertEqual(r29["profiles"],{})
        self.assertEqual(r33["n"],0)
        self.assertEqual(r33["edge_haircut"],1.0)

    def test_pending_backlog_is_quarantined_in_full_before_bounded_validation(self):
        before = self.financials()
        with self.connect() as c:
            result = LI.revalidate_eligible(c,batch_size=2)
            remaining_unsafe = c.execute("""SELECT count(*) AS n FROM v90_learning_episodes
              WHERE learning_eligible=TRUE AND NOT COALESCE(("""+LI.eligible_sql()+"""),FALSE)""").fetchone()["n"]
        self.assertEqual(result["staged"],4)
        self.assertEqual(result["processed"],2)
        self.assertEqual(result["pending"],2)
        self.assertEqual(remaining_unsafe,0)
        rows = self.episodes()
        self.assertFalse(rows["c_unknown"]["learning_eligible"])
        self.assertFalse(rows["d_synthetic"]["learning_eligible"])
        with self.connect() as c:
            complete = LI.revalidate_eligible(c,batch_size=2)
        self.assertEqual(complete["pending"],0)
        self.assertEqual(self.financials(),before)

    def test_failed_sanitizer_clears_cached_profiles_and_reader_gate_stays_closed(self):
        before = self.financials()
        R._v90r29_cache.update(at=10**12,profiles={"unsafe":{"n":100}},summary={"eligible_episodes":100})
        R._v90r33_cache.update(at=10**12,n=100,edge_haircut=.7)
        with patch.object(LI,"revalidate_eligible",side_effect=RuntimeError("validation unavailable")):
            result = self.sanitize()
        self.assertIn("validation unavailable",result["last_error"] or "")
        self.assertEqual(R._v90r29_cache["profiles"],{})
        self.assertEqual(R._v90r33_cache["n"],0)
        self.assertEqual(R._v90r33_cache["edge_haircut"],1.0)
        with self.connect() as c:
            readable = c.execute("SELECT count(*) AS n FROM v90_learning_episodes WHERE "+LI.readable_sql()).fetchone()["n"]
        self.assertEqual(readable,0)
        self.assertEqual(self.financials(),before)

    def test_projection_omits_bar_arrays_and_changed_proof_is_immediately_unreadable(self):
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id='a_clean'""",
              (json.dumps({"timeframe_entry_context":{"bars":[{"close":100}]*1000}}),))
            projected = c.execute("SELECT "+LI.trade_projection_sql()+" FROM paper_trades t WHERE trade_id='a_clean'").fetchone()
            self.assertNotIn("timeframe_entry_context",projected["payload"])
            self.assertIsNone(LI.trade_exclusion(dict(projected)))
        self.sanitize()
        with self.connect() as c:
            original = c.execute("SELECT "+LI.readable_sql()+" AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"]
            self.assertTrue(original)
            c.execute("""UPDATE paper_trades SET payload=jsonb_set(payload,'{data_integrity_status}','"UNKNOWN"') WHERE trade_id='a_clean'""")
            stale = c.execute("SELECT "+LI.readable_sql()+" AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"]
            self.assertFalse(stale)
        result = self.sanitize()
        self.assertIsNone(result["last_error"],result)
        self.assertFalse(self.episodes()["a_clean"]["learning_eligible"])

    def test_numerically_missing_path_and_accounting_are_never_recovered(self):
        with self.connect() as c:
            row = dict(c.execute("SELECT "+LI.trade_projection_sql()+" FROM paper_trades t WHERE trade_id='a_clean'").fetchone())
        self.assertIsNone(LI.trade_exclusion(row))
        for field in ("gross_pnl_rub","fees_rub","funding_rub","net_pnl_rub"):
            for value in (None,float("nan"),float("inf"),True):
                changed=copy.deepcopy(row); changed[field]=value
                self.assertEqual(LI.trade_exclusion(changed),"INCOMPLETE_ACCOUNTING")
        for field in ("mfe_pct","mae_pct"):
            changed=copy.deepcopy(row); changed["payload"][field]=None
            self.assertEqual(LI.trade_exclusion(changed),"INCOMPLETE_OBSERVED_PATH")

    def test_duplicate_observed_event_does_not_restore_two_learning_samples(self):
        self.add_trade("b_duplicate")
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET payload=(SELECT payload FROM paper_trades
              WHERE trade_id='a_clean') WHERE trade_id='b_duplicate'""")
        before=self.financials()
        with self.connect() as c:
            result=LI.revalidate_eligible(c,batch_size=32)
        self.assertEqual(result["verified"],1)
        rows=self.episodes()
        self.assertFalse(rows["b_duplicate"]["learning_eligible"])
        self.assertEqual(rows["b_duplicate"]["payload"]["learning_integrity"]["exclusion_reason"],
                         "DUPLICATE_OBSERVED_EVENT")
        self.assertEqual(self.financials(),before)

if __name__ == "__main__":
    unittest.main()
