"""Actual PostgreSQL staged AMI publication, restart and legacy-score parity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_asset_management_intelligence as AMI
import veritas_scorecard_delivery as DELIVERY
import veritas_learning_state as STORE


DSN = os.getenv("VERITAS_QUALITY_TEST_DSN", "")
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
EPOCH = START.isoformat()
LEARNING = {"status": "MEASURABLE", "index_vs_start": 110., "index_version": "2.1",
            "mode": "INDEPENDENT", "calculated_at": START.isoformat()}


class OneChunkBudget:
    """Expose a deterministic remaining budget; all SQL and storage stay real."""
    def __init__(self):
        self.calls = 0

    def current_budget(self):
        self.calls += 1
        return {"remaining_seconds": 6. if self.calls == 1 else 2.1}


@unittest.skipUnless(DSN, "isolated PostgreSQL test database not configured")
class AMIStagedPipelineSQLTests(unittest.TestCase):
    def setUp(self):
        from test_veritas_learning_state import DurableStateSQLTests
        self.database = DurableStateSQLTests()
        self.database.setUp()
        self.addCleanup(self.database.tearDown)
        self.connect = self.database.connect
        for key, value in (("_CACHE", {"at": 0., "epoch": None, "value": None}),
                           ("_SNAPSHOT", None), ("_RESTORED_EPOCH", None),
                           ("_REFRESH_STATE", {"status": "NOT_STARTED", "last_error": None})):
            item = patch.object(AMI, key, value)
            item.start()
            self.addCleanup(item.stop)
        with self.connect() as c:
            c.execute("""CREATE TABLE ledger_events(
                id BIGSERIAL PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,entity_key TEXT NOT NULL,
                event_type TEXT NOT NULL,event_ts TIMESTAMPTZ NOT NULL,asset TEXT,horizon TEXT,payload JSONB NOT NULL)""")
            c.execute("CREATE INDEX ami_ledger_entity ON ledger_events(entity_key,event_type)")
            c.execute("""CREATE TABLE paper_trades(portfolio_name TEXT,opened_at TIMESTAMPTZ,
                closed_at TIMESTAMPTZ,status TEXT,net_pnl_rub DOUBLE PRECISION,return_on_entry_nav DOUBLE PRECISION)""")
            c.execute("""CREATE TABLE paper_nav_history(portfolio_name TEXT,observed_at TIMESTAMPTZ,nav_rub DOUBLE PRECISION)""")
            c.execute("""CREATE TABLE v90_learning_episodes(closed_at TIMESTAMPTZ,asset TEXT,horizon TEXT,
                regime TEXT,capture_ratio DOUBLE PRECISION,movement_realization_ratio DOUBLE PRECISION,
                giveback_pct DOUBLE PRECISION,primary_attribution TEXT,attributions JSONB,
                net_pnl_rub DOUBLE PRECISION,learning_eligible BOOLEAN)""")
            c.execute("CREATE TABLE knowledge_sources(source_id TEXT PRIMARY KEY)")
            c.execute("CREATE TABLE knowledge_rules(rule_id TEXT PRIMARY KEY)")
            c.execute("""CREATE TABLE knowledge_backtest_oos_stats(rule_id TEXT,sample TEXT,n INTEGER,
                hit_rate DOUBLE PRECISION,avg_signed_return DOUBLE PRECISION)""")
            c.execute("""CREATE TABLE learning_baselines(baseline_key TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,payload JSONB NOT NULL)""")
            self.seed(c)

    def seed(self, c):
        events = []
        for i in range(130):
            key = "case-"+str(i)
            at = START+timedelta(hours=i)
            direction = "SHORT" if i % 3 == 0 else "LONG"
            dp = {"research_decision": direction, "decision": direction,
                  "regime": "DOWN" if direction == "SHORT" else "UP",
                  "agents": [{"direction": direction, "confidence": .7},
                             {"direction": "LONG", "confidence": .2}],
                  "knowledge_shadow_matches": [{"rule_id": "R1"}] if i % 4 == 0 else [],
                  "knowledge_cio_adjustment": {"score_with_experience": .02 if i % 4 == 0 else 0.}}
            outcome = {"forward_return": -.015 if i % 3 == 0 else .02}
            asset = "BTC" if i % 2 else "ETH"
            events.extend((("d-"+key, key, "decision", at, asset, "1h", json.dumps(dp)),
                           ("o-"+key, key, "outcome", at+timedelta(hours=1), asset, "1h", json.dumps(outcome))))
        with c.cursor() as rows:
            rows.executemany("""INSERT INTO ledger_events(event_key,entity_key,event_type,event_ts,asset,horizon,payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)""", events)
            rows.executemany("INSERT INTO paper_trades VALUES(%s,%s,%s,'CLOSED',%s,%s)",
                [("Champion" if i % 2 else "Challenger", START+timedelta(hours=i),
                  START+timedelta(hours=i+1), 100. if i % 3 else -40., .001 if i % 3 else -.0004)
                 for i in range(30)])
            rows.executemany("INSERT INTO paper_nav_history VALUES(%s,%s,%s)",
                [(name, START+timedelta(hours=i), 100000.+i*100.-(400. if i == 5 else 0.))
                 for name in ("Champion", "Challenger") for i in range(12)])
            rows.executemany("INSERT INTO v90_learning_episodes VALUES(%s,'BTC','1h','UP',%s,%s,.12,%s,%s::jsonb,%s,TRUE)",
                [(START+timedelta(hours=i), .25 if i < 20 else .45, .25 if i < 20 else .5,
                  "EDGE_OVERFORECAST" if i < 20 else "GOOD_EXECUTION",
                  json.dumps(["EDGE_OVERFORECAST"] if i < 20 else ["GOOD_EXECUTION"]),
                  -50. if i < 20 else 100.) for i in range(40)])
            rows.executemany("INSERT INTO knowledge_sources VALUES(%s)", [("S"+str(i),) for i in range(12)])
            rows.executemany("INSERT INTO knowledge_rules VALUES(%s)", [("R"+str(i),) for i in range(8)])
            rows.executemany("INSERT INTO knowledge_backtest_oos_stats VALUES(%s,'OOS',30,%s,%s)",
                [("R1", .6, .002), ("R2", .58, .001), ("R3", .4, -.002)])

    def step(self, cursor=None):
        context = SimpleNamespace(sql_timeout_ms=2000, check=lambda: None, lane=OneChunkBudget())
        return AMI.refresh_snapshot(self.connect, LEARNING, EPOCH, context=context, cursor=cursor)

    def work(self):
        with self.connect() as c:
            return STORE.load_snapshot_in_transaction(c, DELIVERY.WORK_SLOT, DELIVERY.WORK_VERSION)

    def test_real_sql_pipeline_matches_legacy_and_resumes_committed_chunk_after_ram_restart(self):
        # Seed the same original rollout baseline before either comparison.
        # This invokes all old SQL readers and the actual scoring function.
        expected = AMI._build_scorecard_unlocked(self.connect, LEARNING, EPOCH, cache_seconds=0, publish=False)
        self.assertIsNone(AMI._SNAPSHOT)
        self.assertEqual(expected["evidence"]["independent_decision_episodes"], 130)
        self.assertEqual(expected["evidence"]["fresh_candidate_trades"]["n"], 30)
        self.assertEqual(expected["evidence"]["learning_episodes"]["n"], 40)
        self.assertEqual(expected["evidence"]["knowledge"]["validated_oos_rules"], 2)
        with self.connect() as c:
            baseline = deepcopy(c.execute("SELECT payload FROM learning_baselines").fetchone()["payload"])

        restore = self.step()
        self.assertEqual(restore["reason"], "SCORECARD_RESTORE_CHECKED")
        sampled = self.step(restore["cursor"])
        self.assertEqual(sampled["stage"], "decisions")
        self.assertEqual(sampled["processed"], 0)
        first = self.step(sampled["cursor"])
        self.assertEqual(first["status"], "PROGRESS")
        self.assertEqual(first["processed"], 64)
        saved = self.work()
        self.assertEqual(saved["payload"]["offset"], 64)
        self.assertEqual(len(saved["payload"]["pairs"]), 130)
        cycle = saved["payload"]["cycle_id"]
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) n FROM veritas_learning_snapshots WHERE name=%s",
                                       (DELIVERY.WORK_SLOT,)).fetchone()["n"], 1)
            self.assertIsNone(STORE.load_snapshot_in_transaction(c, "intelligence_scorecard", AMI.VERSION))

        # Simulate process-local state loss, not a storage mock or regenerated
        # input. The durable workslot is the only source of the next offset.
        AMI._CACHE = {"at": 0., "epoch": None, "value": None}
        AMI._SNAPSHOT = None
        AMI._RESTORED_EPOCH = None
        AMI._REFRESH_STATE = {"status": "NOT_STARTED", "last_error": None}
        restarted = self.step()
        self.assertEqual(restarted["reason"], "SCORECARD_RESTORE_CHECKED")
        resumed = self.step(restarted["cursor"])
        self.assertEqual(resumed["processed"], 128)
        self.assertEqual(resumed["cursor"]["cycle_id"], cycle)
        self.assertIsNone(AMI._SNAPSHOT)
        cursor = resumed["cursor"]
        for _ in range(12):
            result = self.step(cursor)
            cursor = result["cursor"]
            if result["status"] == "OK":
                break
            self.assertIsNone(AMI._SNAPSHOT)
        else:
            self.fail("real staged pipeline did not publish")
        self.assertEqual(result["stage"], "complete")
        self.assertEqual(result["processed"], 130)
        self.assertEqual(AMI._SNAPSHOT[2], expected)
        with self.connect() as c:
            published = STORE.load_snapshot_in_transaction(c, "intelligence_scorecard", AMI.VERSION)
            complete = STORE.load_snapshot_in_transaction(c, DELIVERY.WORK_SLOT, DELIVERY.WORK_VERSION)
            self.assertEqual(published["payload"]["scorecard"], expected)
            self.assertEqual(complete["payload"]["cycle_id"], cycle)
            self.assertEqual(complete["payload"]["offset"], 130)
            self.assertEqual(complete["payload"]["stage"], "complete")
            self.assertEqual(c.execute("SELECT count(*) n FROM veritas_learning_snapshots WHERE name=%s",
                                      (DELIVERY.WORK_SLOT,)).fetchone()["n"], 1)
            self.assertEqual(c.execute("SELECT payload FROM learning_baselines").fetchone()["payload"], baseline)


if __name__ == "__main__":
    unittest.main()
