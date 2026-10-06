"""Paper lessons must retain raw provenance through archive, export and consumption."""
import copy
import json
import os
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import veritas_intelligence as I
import veritas_portfolio as P
import veritas_learning_exports as E
import veritas_learning_integrity as LI
from test_veritas_strategy_quality_sql import observed_evidence

NOW = datetime(2026, 10, 6, 19, tzinfo=timezone.utc)
DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")


def trade(key="A", net=30., event="STF_SHARED"):
    p = observed_evidence("ETH", "LONG", event, NOW, "1h")
    p.update(canonical_setup_id=event, setup_family="TREND", regime="TREND",
             mfe_pct=1.5, mae_pct=-.8, telemetry_completeness=1.,
             stop_price=98., take_price=104., exit_reason="TAKE_PROFIT",
             learning_eligible=True)
    return dict(trade_id=key, portfolio_name="Champion", asset="ETH", horizon="1h",
                direction="LONG", status="CLOSED", opened_at=NOW, closed_at=NOW+timedelta(hours=1),
                setup="TREND", avg_entry_price=100., avg_exit_price=101.,
                return_on_entry_nav=net/1_000_000., gross_pnl_rub=net+10.,
                fees_rub=8., funding_rub=2., net_pnl_rub=net, payload=p,
                learning_evidence_hash="hash:"+key)


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows); self.calls = []
    def execute(self, sql, args=()):
        self.calls.append((sql, args)); return self
    def fetchall(self):
        return self.rows
    def fetchone(self):
        return self.rows[0] if self.rows else None


@contextmanager
def connected(cursor):
    yield cursor


def flattened(rows):
    cursor = Cursor(rows)
    with patch.object(P, "ensure_schema"):
        return P._v90j_load_closed(lambda: connected(cursor))


def grouped(rows):
    return P._v90j_unique_learning(flattened(rows))[0]


def lesson(group, at=None):
    return dict(event_type="experience_lesson", event_ts=at or datetime.now(timezone.utc),
                asset=group["asset"], horizon=group["horizon"], payload={
                    **E.export_metadata(group), "source": E.PAPER_SOURCE,
                    "setup_id": group["episode_key"], "setup_family": "TREND", "regime_bucket": "TREND",
                    "entry_state": "NORMAL", "direction": group["direction"],
                    "learning_weight": .20, "actual_pnl_fraction": group["avg_return_pct"]/100,
                    "profitable": group["avg_return_pct"] > 0, "label": "GOOD_EXECUTION"})


class LearningExportTests(unittest.TestCase):
    def setUp(self):
        self.saved_board = getattr(I.setup_memory_board, "_cache", None)
        self.saved_lookup = getattr(I._v842_memory_lookup, "_cache", None)
        E.invalidate_memory(vars(I))

    def tearDown(self):
        I.setup_memory_board._cache = self.saved_board
        I._v842_memory_lookup._cache = self.saved_lookup

    def test_archive_checks_original_path_before_shadow_telemetry_recovery(self):
        raw = trade()
        raw["payload"].pop("mfe_pct"); raw["payload"].pop("mae_pct")
        raw.update(shadow_high_price=110., shadow_low_price=90.)
        row = flattened([raw])[0]
        self.assertTrue(row["telemetry_recovered_from_shadow"])
        self.assertIsNotNone(row["mfe_pct"])
        self.assertFalse(row["learning_eligible"])
        self.assertEqual(row["learning_exclusion_reason"], "INCOMPLETE_OBSERVED_PATH")

    def test_original_lifetime_path_is_used_before_any_shadow_backfill(self):
        raw = trade()
        raw["payload"].pop("mfe_pct"); raw["payload"].pop("mae_pct")
        raw["payload"].update(r55_lifetime_mfe_pct=2., r55_lifetime_mae_pct=-.5)
        raw.update(shadow_high_price=150., shadow_low_price=50.)
        row = flattened([raw])[0]
        self.assertTrue(row["learning_eligible"])
        self.assertEqual(row["mfe_pct"], 2.)
        self.assertEqual(row["mae_pct"], -.5)

    def test_archive_does_not_resurrect_false_unknown_or_nonfinite_evidence(self):
        for kind in ("false", "source", "event", "accounting"):
            with self.subTest(kind=kind):
                raw = trade()
                if kind == "false": raw["payload"]["learning_eligible"] = False
                elif kind == "source": raw["payload"]["data_integrity_status"] = "UNKNOWN"
                elif kind == "event": raw["payload"]["entry_event_snapshot"]["confirmed_at"] = None
                else: raw["fees_rub"] = float("nan")
                self.assertFalse(flattened([raw])[0]["learning_eligible"])

    def test_invalid_losing_copy_excludes_whole_group_without_removing_its_loss(self):
        winner, loser = trade("A", 100.), trade("B", -300.)
        loser["payload"]["data_integrity_status"] = "UNKNOWN"
        group = grouped([winner, loser])
        self.assertEqual(group["total_net_pnl_rub"], -200.)
        self.assertEqual(group["trade_count"], 2)
        self.assertFalse(group["learning_eligible"])
        self.assertEqual(group["learning_weight"], 0.)
        self.assertEqual(group["learning_trade_ids"], ["A", "B"])

    def test_clean_group_exports_all_raw_trade_ids_and_hashes(self):
        group = grouped([trade("A", 100.), trade("B", -20.)])
        self.assertTrue(E.exportable(group))
        self.assertEqual(group["learning_trade_ids"], ["A", "B"])
        self.assertEqual(group["learning_evidence_hashes"], {"A": "hash:A", "B": "hash:B"})
        self.assertEqual(group["learning_observed_event_id"], "STF_SHARED")

    def test_publisher_ignores_old_true_flags_and_clears_cache_when_eligible_zero(self):
        old = dict(episode_key="OLD", learning_eligible=True, learning_weight=.35)
        cursor = Cursor()
        I.setup_memory_board._cache = (time.time(), {"items": ["old"]})
        I._v842_memory_lookup._cache = ("old", {"unsafe": True})
        with patch.object(I, "pg_enabled", return_value=True), \
             patch.object(I.VP, "learning_archive", return_value=[old]), \
             patch.object(I, "pg_connect", side_effect=lambda: connected(cursor)):
            result = I._v90_publish_paper_execution_lessons()
        self.assertEqual(result["learning_fallback"], 0)
        self.assertEqual(cursor.calls, [])
        self.assertIsNone(I.setup_memory_board._cache)
        self.assertIsNone(I._v842_memory_lookup._cache)

    def test_verified_paper_export_is_not_suppressed_by_old_shadow_and_keeps_close_time(self):
        group = grouped([trade()])
        cursor = Cursor([{"shadow_exists": 1}])
        with patch.object(I, "pg_enabled", return_value=True), \
             patch.object(I.VP, "learning_archive", return_value=[group]), \
             patch.object(I, "pg_connect", side_effect=lambda: connected(cursor)):
            result = I._v90_publish_paper_execution_lessons()
        self.assertEqual(result["learning_fallback"], 1)
        writes = [args for sql, args in cursor.calls if "INSERT INTO ledger_events" in sql]
        self.assertEqual(len(writes), 2)
        args = next(args for args in writes if args[0].startswith("experience_lesson:"))
        payload = json.loads(args[5])
        self.assertEqual(payload["learning_trade_ids"], ["A"])
        self.assertEqual(payload["learning_evidence_version"], LI.VERSION)
        self.assertEqual(payload["learning_export_version"], E.VERSION)
        self.assertEqual(args[2], group["last_closed_at"])
        self.assertEqual(payload["learning_weight"], .20)
        self.assertFalse(any("CANONICAL_SHADOW_TRADE" in sql for sql, _ in cursor.calls))

    def test_old_or_expired_memory_cache_cannot_keep_influencing_fast_path(self):
        for data, at in (({"items": ["old"]}, time.time()),
                         (dict(E.memory_contract(), items=["expired"]), 0.)):
            with self.subTest(data=data):
                I.setup_memory_board._cache = (at, data)
                self.assertEqual(I.setup_memory_board()["status"], "BACKGROUND_PENDING")
                self.assertIsNone(I.setup_memory_board._cache)
        good = dict(E.memory_contract(), items=[])
        I.setup_memory_board._cache = (time.time(), good)
        self.assertIs(I.setup_memory_board(), good)

    def test_failed_sanitizer_generation_revokes_cached_learning_immediately(self):
        cached = dict(E.memory_contract(), items=["old verified statistics"])
        I.setup_memory_board._cache = (time.time(), cached)
        I._v842_memory_lookup._cache = ("old", {"unsafe": True})
        cursor = Cursor()
        with patch.object(cursor, "execute", side_effect=RuntimeError("database unavailable")):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                LI.revalidate_eligible(cursor)
        self.assertEqual(I.setup_memory_board()["status"], "BACKGROUND_PENDING")
        self.assertIsNone(I._v842_memory_lookup._cache)

    def test_source_unverified_shadow_losses_and_wins_cannot_veto_or_reinforce(self):
        for pnl in (-.01, .02):
            rows = [dict(total_pnl_fraction=pnl, payload={}) for _ in range(20)]
            cursor = Cursor(rows)
            with self.subTest(pnl=pnl), patch.object(I, "pg_enabled", return_value=True), \
                 patch.object(I, "pg_connect", side_effect=lambda: connected(cursor)):
                result = I.profitability_gate("ETH", "1h", "LONG", {"regime": "TREND"},
                                              {"eligible": True, "setup": "TREND"})
            self.assertTrue(result["allow"])
            self.assertEqual(result["size_multiplier"], 1.)
            self.assertEqual(result["status"], "DIAGNOSTIC_ONLY")
            self.assertFalse(result["profile"]["decision_influence"])
            self.assertFalse(result["profile"]["net_economics_verified"])

    def test_source_unverified_shadow_winners_cannot_set_live_pullback_buffer(self):
        rows = [dict(total_pnl_fraction=.02, entry_price=100., high_price=110., low_price=98., payload={})
                for _ in range(20)]
        with patch.object(I, "pg_enabled", return_value=True), \
             patch.object(I, "pg_connect", side_effect=lambda: connected(Cursor(rows))):
            result = I.trade_path_intelligence("ETH", "1h", "LONG", {"setup": "TREND"})
        self.assertFalse(result["decision_influence"])
        self.assertEqual(result["management_policy"], "BASELINE")
        self.assertIsNone(result["normal_pullback_buffer_pct"])
        self.assertEqual(result["profile"]["n"], 20)

    def test_unverified_shadow_close_does_not_veto_new_paper_admission(self):
        setup = I._v77_new_setup_signature("ETH", "1h", "LONG", "TREND", 100.)
        recent = {"closed_at": datetime.now(timezone.utc)-timedelta(minutes=1),
                  "payload": {"setup_id": setup, "trigger_level": 100.}}
        plan = {"eligible": True, "setup": "TREND", "entry_price": 100., "trigger_level": 100.,
                "stop_price": 98., "stop_distance_pct": .02, "expected_move_pct": .04,
                "expected_to_stop_ratio": 2., "initial_position_fraction": .25, "structural_stop_enforced": True}
        with patch.object(I, "_v77_recent_closed_trade", return_value=recent):
            result = I.v77_decision_quality_stack("ETH", "1h", {"price": 100., "score": .8}, plan, "LONG")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["reentry_intelligence"]["allowed"])
        self.assertEqual(result["stop_price"], 98.)

    def test_verified_paper_memory_keeps_existing_bounded_sizing_controls(self):
        for net, expected in ((30., 1.20), (-30., .55)):
            rows = [lesson(grouped([trade(str(i), net, "STF_"+str(i))])) for i in range(60)]
            with self.subTest(net=net), patch.object(I, "pg_enabled", return_value=True), \
                 patch.object(I, "pg_connect", side_effect=lambda: connected(Cursor())), \
                 patch.object(E, "decision_lessons", return_value=rows):
                I.setup_memory_board(force=True)
                memory = I.setup_memory_profile("ETH", "1h", {"regime": "TREND"}, "LONG", {})
                policy = I.execution_policy_v84("ETH", "1h", {"regime": "TREND"}, "LONG", {},
                                                memory, {"size_multiplier": 1.})
            self.assertTrue(memory["decision_influence"])
            self.assertAlmostEqual(policy["size_multiplier"], expected)
            self.assertFalse(policy["hard_gate_override"])


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class LearningExportSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver, self.row_factory = psycopg, dict_row
        self.schema = "learning_export_test_"+uuid.uuid4().hex
        self.saved_board = getattr(I.setup_memory_board, "_cache", None)
        self.saved_lookup = getattr(I._v842_memory_lookup, "_cache", None)
        E.invalidate_memory(vars(I))
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
              primary_attribution text,attributions jsonb,learning_action text,
              learning_eligible boolean,payload jsonb)""")
            c.execute("""CREATE TABLE ledger_events(
              event_key text PRIMARY KEY,entity_key text,event_type text,event_ts timestamptz,
              asset text,horizon text,payload jsonb)""")

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            c.execute(f"SET search_path TO {self.schema}")
            yield c

    def tearDown(self):
        I.setup_memory_board._cache = self.saved_board
        I._v842_memory_lookup._cache = self.saved_lookup
        with self.driver.connect(DSN) as c:
            c.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def add_trade(self, raw):
        with self.connect() as c:
            c.execute("INSERT INTO paper_trades VALUES ("+",".join(["%s"]*11)+",%s::jsonb)",
                      tuple(raw[k] for k in LI.TRADE_FIELDS)+(json.dumps(raw["payload"]),))
            c.execute("""INSERT INTO v90_learning_episodes VALUES
              (%s,%s,%s,%s,%s,'GOOD_EXECUTION','["GOOD_EXECUTION"]','RETAIN_RULE',TRUE,'{}')""",
                      tuple(raw[k] for k in ("trade_id","asset","direction","horizon","closed_at")))

    def group(self):
        with self.connect() as c:
            rows = [dict(row) for row in c.execute(
                "SELECT "+LI.trade_projection_sql()+","+LI.evidence_hash_sql()+" AS learning_evidence_hash FROM paper_trades t").fetchall()]
        for row in rows:
            row.update(episode_key="STF_SHARED", setup="TREND", setup_family="TREND", regime="TREND",
                       entry_state="NORMAL", horizon_state="UNKNOWN", return_pct=100*row["net_pnl_rub"]/1_000_000.,
                       mfe_pct=row["payload"]["mfe_pct"], mae_pct=row["payload"]["mae_pct"],
                       telemetry_completeness=1., learning_label="GOOD_EXECUTION", learning_eligible=True)
            E.mark_trade(row, row)
        return P._v90j_unique_learning(rows)[0]

    def put_lesson(self, item, suffix="current"):
        with self.connect() as c:
            c.execute("INSERT INTO ledger_events VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
                      ("experience_lesson:paper_exec:"+suffix, "paper_exec:"+suffix,
                       item["event_type"], item["event_ts"], item["asset"], item["horizon"],
                       json.dumps(item["payload"])))

    def board(self):
        with patch.object(I,"pg_enabled",return_value=True), patch.object(I,"pg_connect",side_effect=self.connect):
            result = I.setup_memory_board(force=True)
        self.assertEqual(result.get("status"), "OK", result)
        return result

    def test_legacy_paper_and_shadow_labels_are_excluded_before_memory_limit(self):
        self.add_trade(trade())
        with self.connect() as c:
            LI.revalidate_eligible(c)
        good = lesson(self.group(), datetime.now(timezone.utc)-timedelta(days=1))
        self.put_lesson(good)
        for i, source in enumerate(("CANONICAL_SHADOW_TRADE", E.PAPER_SOURCE)):
            old = copy.deepcopy(good)
            old["payload"] = {"source": source, "direction": "LONG", "learning_weight": 1.,
                              "profitable": True, "actual_pnl_fraction": .10, "setup_family": "TREND"}
            self.put_lesson(old, "old"+str(i))
        with self.connect() as c:
            read = E.decision_lessons(c, 1)
        self.assertEqual(len(read), 1)
        self.assertEqual(read[0]["payload"]["learning_export_version"], E.VERSION)
        exact = [row for row in self.board()["items"] if row["level"] == "EXACT"]
        self.assertEqual(exact[0]["n"], 1)

    def test_current_lesson_needs_current_eligible_episode_and_current_raw_fingerprint(self):
        self.add_trade(trade())
        self.put_lesson(lesson(self.group()))
        self.assertEqual(self.board()["items"], [])
        with self.connect() as c:
            LI.revalidate_eligible(c)
        self.assertTrue(self.board()["items"])
        with self.connect() as c:
            c.execute("UPDATE paper_trades SET net_pnl_rub=net_pnl_rub+1 WHERE trade_id='A'")
        self.assertEqual(self.board()["items"], [])
        with self.connect() as c:
            LI.revalidate_eligible(c)
        # Revalidating the trade must not silently approve the old exported result.
        self.assertEqual(self.board()["items"], [])

    def test_valid_duplicate_copies_are_one_lesson_but_invalid_member_revokes_it(self):
        self.add_trade(trade("A", 100.)); self.add_trade(trade("B", -20.))
        with self.connect() as c:
            LI.revalidate_eligible(c)
        item = lesson(self.group())
        self.put_lesson(item)
        self.put_lesson(copy.deepcopy(item), "same-event-other-key")
        exact = [row for row in self.board()["items"] if row["level"] == "EXACT"]
        self.assertEqual(exact[0]["n"], 1)
        with self.connect() as c:
            c.execute("""UPDATE paper_trades SET payload=jsonb_set(payload,'{data_integrity_status}','"UNKNOWN"')
                         WHERE trade_id='B'""")
        self.assertEqual(self.board()["items"], [])
        with self.connect() as c:
            total = c.execute("SELECT sum(net_pnl_rub) AS net FROM paper_trades").fetchone()["net"]
        self.assertEqual(total, 80.)

    def test_manually_ineligible_representative_or_old_validation_version_cannot_learn(self):
        self.add_trade(trade())
        with self.connect() as c:
            LI.revalidate_eligible(c)
        self.put_lesson(lesson(self.group()))
        with self.connect() as c:
            c.execute("UPDATE v90_learning_episodes SET learning_eligible=FALSE WHERE trade_id='A'")
        self.assertEqual(self.board()["items"], [])
        with self.connect() as c:
            c.execute("""UPDATE v90_learning_episodes SET learning_eligible=TRUE,
              payload=jsonb_set(payload,'{learning_integrity,version}','"OLD"') WHERE trade_id='A'""")
        self.assertEqual(self.board()["items"], [])


if __name__ == "__main__":
    unittest.main()
