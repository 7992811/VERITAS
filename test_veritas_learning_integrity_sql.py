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
from test_veritas_readiness_fixtures import add_observed_path
from test_veritas_trade_diagnostics import closed_trade
import veritas_trade_diagnostics as DIAGNOSTICS

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

    @contextmanager
    def autocommit_connect(self):
        # Match the production connection factory, not the default test driver.
        with self.driver.connect(DSN, autocommit=True, row_factory=self.row_factory) as c:
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
                 strategy_entry_sha="original-entry-sha", telemetry_completeness=1.0,
                 exit_reason="TAKE_PROFIT" if net > 0 else "MODEL_CLOSE")
        raw = add_observed_path(dict(trade_id=key,asset=asset,direction="LONG",horizon="1h",
                                    status="CLOSED",opened_at=opened,closed_at=opened+timedelta(hours=1),
                                    gross_pnl_rub=net+10,fees_rub=8,funding_rub=2,net_pnl_rub=net,payload=p))
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
        self.add_raw_trade(raw,eligible=kind!="manual")

    def add_raw_trade(self, raw, *, eligible=True, attribution="GOOD_EXECUTION"):
        with self.connect() as c:
            c.execute("INSERT INTO paper_trades VALUES ("+",".join(["%s"]*len(LI.TRADE_FIELDS))+",%s::jsonb)",
                      tuple(raw[k] for k in LI.TRADE_FIELDS)+(json.dumps(raw["payload"]),))
            c.execute("""INSERT INTO v90_learning_episodes VALUES
              (%s,%s,%s,%s,%s,'BREAKOUT','TREND',%s,1.0,.7,%s,%s::jsonb,'RETAIN_RULE',%s,%s::jsonb)""",
              tuple(raw[k] for k in ("trade_id","asset","direction","horizon","closed_at","net_pnl_rub"))+
              (attribution,json.dumps([attribution]),eligible,
               json.dumps({"independent_episode_key":raw["trade_id"],"original_note":"preserve"})))

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
        self.assertEqual(rows["a_clean"]["primary_attribution"], "VALID_PROFITABLE_TRADE")
        self.assertEqual(rows["a_clean"]["payload"]["trade_diagnostics"]["version"], DIAGNOSTICS.VERSION)
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

    def test_production_autocommit_rolls_back_the_whole_failed_revalidation(self):
        before,episodes = self.financials(),self.episodes()
        revalidate = LI.revalidate_eligible
        def fail_after_updates(connection):
            setting=connection.execute("SHOW statement_timeout").fetchone()['statement_timeout']
            self.assertEqual(setting,'4s')
            result=revalidate(connection)
            self.assertGreater(result['processed'],0)
            raise RuntimeError('test failure after reclassification')
        with patch.object(R,'_v90r29_ensure'),patch.object(LI,'revalidate_eligible',side_effect=fail_after_updates):
            result=R._v90r44_sanitize_learning(self.autocommit_connect,force=True)
        self.assertIn('test failure after reclassification',result['last_error'])
        self.assertEqual(self.episodes(),episodes)
        self.assertEqual(self.financials(),before)
        with patch.object(R,'_v90r29_ensure'):
            result=R._v90r44_sanitize_learning(self.autocommit_connect,force=True)
        self.assertIsNone(result['last_error'],result)
        self.assertTrue(self.episodes()['a_clean']['learning_eligible'])
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

    def test_missing_accounting_or_observation_witness_cannot_be_recovered_from_old_metrics(self):
        with self.connect() as c:
            row = dict(c.execute("SELECT "+LI.trade_projection_sql()+" FROM paper_trades t WHERE trade_id='a_clean'").fetchone())
        self.assertIsNone(LI.trade_exclusion(row))
        for field in ("gross_pnl_rub","fees_rub","funding_rub","net_pnl_rub"):
            for value in (None,float("nan"),float("inf"),True):
                changed=copy.deepcopy(row); changed[field]=value
                self.assertEqual(LI.trade_exclusion(changed),"INCOMPLETE_ACCOUNTING")
        for field in ("mfe_pct","mae_pct"):
            changed=copy.deepcopy(row); changed["payload"][field]=None
            # Mutable percentages are not the recorded original quote witness.
            self.assertIsNone(LI.trade_exclusion(changed))
        changed=copy.deepcopy(row); changed["payload"].pop("observation_path")
        self.assertEqual(LI.trade_exclusion(changed),"MISSING_OBSERVATION_PATH")

    def test_prior_stop_error_is_recomputed_as_valid_loss_and_never_restored(self):
        raw = closed_trade(favorable_r=1.2)
        raw["trade_id"] = "loss_with_valid_initial_stop"
        raw["payload"].update(learning_label="RIGHT_DIRECTION_STOP_ERROR",
                              learning_conclusion="LEGACY_WIDEN_STOP_VERDICT")
        self.add_raw_trade(raw,attribution="RIGHT_DIRECTION_STOP_ERROR")
        before = self.financials()
        result = self.sanitize()
        self.assertIsNone(result["last_error"],result)
        saved = self.episodes()[raw["trade_id"]]
        self.assertTrue(saved["learning_eligible"])
        self.assertEqual(saved["primary_attribution"],"VALID_STRUCTURAL_STOP_LOSS")
        self.assertEqual(saved["learning_action"],"COUNT_STRATEGY_OUTCOME")
        self.assertNotIn("RIGHT_DIRECTION_STOP_ERROR",saved["attributions"])
        diagnosis=saved["payload"]["trade_diagnostics"]
        self.assertEqual(diagnosis["violations"],[])
        self.assertFalse(diagnosis["directional_error"])
        self.assertAlmostEqual(diagnosis["normalization"]["mfe_r"],1.2)
        self.assertAlmostEqual(diagnosis["normalization"]["mae_r"],-1.)
        # The obsolete verdict survives only as prior audit history, never as
        # the current learning attribution or an instruction to widen a stop.
        self.assertEqual(saved["payload"]["learning_integrity"]["prior_primary_attribution"],
                         "RIGHT_DIRECTION_STOP_ERROR")
        self.assertIsNone(self.sanitize()["last_error"])
        self.assertEqual(self.episodes()[raw["trade_id"]],saved)
        self.assertEqual(self.financials(),before)

    def test_missing_original_geometry_or_path_never_promotes_stale_positive_flags(self):
        cases = (
            ("first_fill",lambda p:p["entry_execution_model"].pop("fill_price")),
            ("initial_stop",lambda p:p.pop("initial_stop_price")),
            ("entry_atr",lambda p:p.pop("entry_atr")),
            ("observation_path",lambda p:p.pop("observation_path")),
            ("path_gap",lambda p:p["observation_path"].update(max_gap_seconds=120.,gap_count=1)),
            ("event_geometry",lambda p:p["entry_event_snapshot"].pop("stop_anchor")),
        )
        for key, mutate in cases:
            raw=closed_trade()
            raw["trade_id"]="missing_"+key
            mutate(raw["payload"])
            self.assertIsNotNone(LI.trade_exclusion(raw))
            self.add_raw_trade(raw,attribution="GOOD_EXECUTION")
        before=self.financials()
        self.assertIsNone(self.sanitize()["last_error"])
        saved=self.episodes()
        for key,_ in cases:
            row=saved["missing_"+key]
            self.assertFalse(row["learning_eligible"],key)
            self.assertEqual(row["payload"]["learning_integrity"]["status"],"EXCLUDED")
            self.assertNotEqual(row["primary_attribution"],"GOOD_EXECUTION")
        self.assertIsNone(self.sanitize()["last_error"])
        self.assertEqual(self.episodes(),saved)
        self.assertEqual(self.financials(),before)

    def test_first_fill_stop_atr_path_and_timeframe_are_bound_to_readable_hash(self):
        self.assertIsNone(self.sanitize()["last_error"])
        with self.connect() as c:
            original=dict(c.execute("SELECT * FROM paper_trades WHERE trade_id='a_clean'").fetchone())
            original_hash=c.execute("SELECT "+LI.evidence_hash_sql()+
                                    " AS h FROM paper_trades t WHERE trade_id='a_clean'").fetchone()["h"]
            mutations=(
                ("fill",lambda p:p["entry_execution_model"].update(fill_price=101.)),
                ("stop",lambda p:p.update(initial_stop_price=p["initial_stop_price"]-.1)),
                ("atr",lambda p:p.update(entry_atr=p["entry_atr"]+.1)),
                ("path",lambda p:p["observation_path"].update(max_price=102.)),
                ("tf",lambda p:p.update(execution_timeframe="5m")),
            )
            for key,mutate in mutations:
                with self.subTest(field=key):
                    p=copy.deepcopy(original["payload"]);mutate(p)
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='a_clean'",(json.dumps(p),))
                    changed_hash=c.execute("SELECT "+LI.evidence_hash_sql()+
                                           " AS h FROM paper_trades t WHERE trade_id='a_clean'").fetchone()["h"]
                    self.assertNotEqual(changed_hash,original_hash)
                    self.assertFalse(c.execute("SELECT "+LI.readable_sql()+
                        " AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"])
                    c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id='a_clean'",
                              (json.dumps(original["payload"]),))
            c.execute("UPDATE paper_trades SET horizon='5m' WHERE trade_id='a_clean'")
            self.assertFalse(c.execute("SELECT "+LI.readable_sql()+
                " AS ok FROM v90_learning_episodes WHERE trade_id='a_clean'").fetchone()["ok"])
            c.execute("UPDATE paper_trades SET horizon=%s WHERE trade_id='a_clean'",(original["horizon"],))
            restored=dict(c.execute("SELECT * FROM paper_trades WHERE trade_id='a_clean'").fetchone())
            self.assertEqual(restored,original)

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
