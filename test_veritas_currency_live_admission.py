"""Offline whole-account authority tests with real LIVE gates and transactional SQL.

Synthetic audited artifacts are labelled as fixtures and never leave the test
database.  No strategy, economics floor, or LIVE risk policy is mocked/relaxed.
SQLite translates PostgreSQL types/schema names only, as in approval-ledger tests.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone
from decimal import Decimal as D
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import veritas_currency_live_admission as A
import veritas_currency_trading as TRADING
import veritas_currency_trade_plan as P
from test_veritas_currency_notifications import Connection as BaseConnection
from test_veritas_currency_trading import Fixtures, ACCOUNT, UID


OTHER = "22222222-2222-4222-8222-222222222222"
ISSUER_KEY = b"offline-independent-live-evidence-issuer-key"


def money(value, currency=None):
    value = D(str(value))
    units = int(value)
    result = {"units": str(units), "nano": int((value-units)*1000000000)}
    if currency:
        result["currency"] = currency
    return result


class Connection(BaseConnection):
    def execute(self, sql, params=()):
        return super().execute(sql.replace("public.", ""), params)


class Broker:
    """Every callable is read-only; deliberately has no submit/cancel method."""
    environment = "production"

    def __init__(self, spec, quote, account):
        self.spec, self.quote, self.account = spec, quote, account
        self.equity = D("1000000")
        self.holdings = []
        self.cash = [money(1000000, "rub")]
        self.orders = []
        self.stops = []
        self.reads = []
        self.after_second_positions = None

    def get_accounts(self):
        self.reads.append("accounts")
        return [{"id": self.account.account_id, "status": "ACCOUNT_STATUS_OPEN", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"}]

    def get_future(self, uid):
        self.reads.append(("future", uid))
        if uid == self.spec.instrument_uid:
            return {"uid": uid, "ticker": "CNYRUBf", "currency": "rub", "realExchange": "REAL_EXCHANGE_MOEX",
                    "lot": self.spec.lot_size, "minPriceIncrement": money(self.spec.tick_size),
                    "minPriceIncrementAmount": money(self.spec.tick_value_rub), "apiTradeAvailableFlag": True,
                    "buyAvailableFlag": True, "sellAvailableFlag": True}
        return {"uid": uid, "ticker": "OTHER_TEST", "currency": "rub", "realExchange": "REAL_EXCHANGE_MOEX",
                "lot": 2, "minPriceIncrement": money("0.5"), "minPriceIncrementAmount": money("5")}

    def get_portfolio(self, account):
        self.reads.append(("portfolio", account))
        return {"accountId": account, "totalAmountPortfolio": money(self.equity, "rub"),
                "positions": [{"instrumentUid": row["instrumentUid"], "instrumentType": row.get("kind", "futures"),
                               "quantity": money(row["balance"])} for row in self.holdings]}

    def get_positions(self, account):
        self.reads.append(("positions", account))
        result = {"accountId": account, "limitsLoadingInProgress": False, "futures": [], "securities": [],
                  "options": [], "money": deepcopy(self.cash), "blocked": []}
        for row in self.holdings:
            result[row.get("kind", "futures")].append({key: value for key, value in row.items() if key != "kind"})
        if self.after_second_positions and sum(item == ("positions", account) for item in self.reads) % 2 == 0:
            self.after_second_positions(result)
        return result

    def list_orders(self, account):
        self.reads.append(("orders", account))
        return deepcopy(self.orders)

    def list_stop_orders(self, account, *, status):
        self.reads.append(("stops", account, status))
        return deepcopy(self.stops)

    def get_order_book(self, uid, depth):
        self.reads.append(("book", uid))
        return {"instrumentUid": uid, "orderbookTs": self.quote.observed_at.isoformat(),
                "bids": [{"price": money(99), "quantity": "100"}], "asks": [{"price": money(100), "quantity": "100"}]}


class AuthorityTests(Fixtures, unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"VERITAS_LIVE_EXECUTION_ENABLED": "1", "VERITAS_LIVE_EXECUTION_ARMED": "1",
            "RENDER_GIT_COMMIT": "1"*40})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.setup_facts()
        self.terms = self.entry()
        self.facts = TRADING.TradeFacts(self.spec, self.account, self.quote)
        self.broker = Broker(self.spec, self.quote, self.account)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/"live.sqlite")
        self.connect = lambda: Connection(self.path)
        self.evidence = A.LiveAdmissionEvidenceRepository(self.connect, ISSUER_KEY, clock=lambda: self.now)
        self.authority = A.create_live_admission(self.connect, self.broker, ACCOUNT, lambda: self.now,
                                                evidence=self.evidence)

    def manifest(self, domains):
        manifest, artifacts = {}, {}
        for key, origin in domains.items():
            blob = ("OFFLINE SYNTHETIC AUDIT FIXTURE: "+key).encode()
            digest = hashlib.sha256(blob).hexdigest()
            manifest[key] = {"origin": origin, "sha256": digest}
            artifacts[digest] = blob
        return manifest, artifacts

    def model_document(self):
        binding = A.model_binding(self.terms)
        manifest, artifacts = self.manifest(A.MODEL_DOMAINS)
        promotion = {"model_version": self.terms["model_version"], "oos_n": 100, "oos_expectancy": .25,
                     "oos_profit_factor": 1.8, "vault_n": 50, "vault_expectancy": .2, "vault_profit_factor": 1.5,
                     "high_cost_expectancy": .1, "calibration_n": 100, "ece": .05,
                     "shadow_trades": 50, "shadow_expectancy": .15, "shadow_max_drawdown": .03,
                     "code_ci_pass": True, "data_parity_pass": True}
        doc = {"schema": A.EVIDENCE_SCHEMA, "kind": "MODEL", "binding": binding,
               "observed_at": self.now.isoformat(), "valid_until": (self.now+timedelta(hours=1)).isoformat(),
               "artifacts": manifest, "payload": {"promotion": promotion, "calibration": {
                   "method": "EMPIRICAL_HELD_OUT_COHORT", "population_binding_sha256": A._hash(binding),
                   "n": 100, "successes": 80}}}
        return doc, artifacts

    def account_document(self):
        snapshot, *_ = self.authority._snapshot(self.terms, self.facts, self.now)
        local = self.now.astimezone(ZoneInfo("Europe/Moscow"))
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        week = day-timedelta(days=day.weekday())
        epoch = week-timedelta(weeks=4)
        manifest, artifacts = self.manifest(A.ACCOUNT_DOMAINS)
        doc = {"schema": A.EVIDENCE_SCHEMA, "kind": "ACCOUNT_HISTORY", "binding": snapshot["binding"],
               "observed_at": self.now.isoformat(), "valid_until": (self.now+timedelta(seconds=30)).isoformat(),
               "artifacts": manifest, "payload": {"method": "BROKER_CASHFLOW_ADJUSTED_UNIT_NAV",
                   "snapshot_sha256": A._hash(snapshot), "cashflows_reconciled": True,
                   "cashflows_reconciled_through": self.now.isoformat(), "equity_rub": snapshot["equity_rub"],
                   "units_outstanding": snapshot["equity_rub"], "day_start_at": day.isoformat(),
                   "week_start_at": week.isoformat(), "risk_epoch_start_at": epoch.isoformat(),
                   "history_covered_from": epoch.isoformat(), "day_start_unit_nav": "1", "week_start_unit_nav": "1",
                   "high_water_unit_nav": "1", "kill_switch": False}}
        return doc, artifacts

    def publish(self, doc, artifacts):
        review = {"reviewer": "offline-independent-reviewer", "audit_id": "synthetic-test-audit",
                  "reviewed_at": self.now.isoformat(), "decision": "ACCEPT_EVIDENCE"}
        payload = A.audited_evidence_payload(self.terms, doc, artifacts, review)
        if doc["kind"] == "MODEL":
            # A real previous-deployment certificate can be archived; its full
            # binding still differs from today's issuer lookup and cannot pass.
            payload["scope"]["model_binding"] = deepcopy(doc["binding"])
        signature = hmac.new(ISSUER_KEY, A.canonical_json(payload).encode(), hashlib.sha256).hexdigest()
        return self.evidence.publish(payload, signature, now=self.now)

    def ready(self, *, model_change=None, account_change=None):
        doc, artifacts = self.model_document()
        if model_change:
            model_change(doc, artifacts)
        self.publish(doc, artifacts)
        doc, artifacts = self.account_document()
        if account_change:
            account_change(doc, artifacts)
        self.publish(doc, artifacts)

    def authorize(self):
        return self.authority(terms=self.terms, facts=self.facts, now=self.now)

    def add_other_future(self):
        self.broker.holdings.append({"instrumentUid": OTHER, "balance": "2", "blocked": "0"})
        self.broker.stops.append({"stopOrderId": "synthetic-protective-stop", "instrumentUid": OTHER,
            "status": "STOP_ORDER_STATUS_ACTIVE", "orderType": "STOP_ORDER_TYPE_STOP_LOSS", "lotsRequested": "1",
            "direction": "STOP_ORDER_DIRECTION_SELL", "stopPrice": money(900, "rub")})

    def correlation(self, doc, artifacts):
        manifest, blobs = self.manifest({"correlations": "VERSIONED_RETURN_CORRELATION"})
        doc["artifacts"].update(manifest)
        artifacts.update(blobs)
        doc["payload"]["correlations"] = {"observations": 100, "matrix": {"CNYRUBF": {"TBANK:"+OTHER: .8}}}

    def test_real_whole_equity_and_promotion_pass_without_changing_any_live_policy(self):
        self.ready()
        result = self.authorize()
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["account_equity_rub"], "1000000")
        self.assertAlmostEqual(result["account_risk"]["fraction_nav_after"], 24690/1000000)
        self.assertEqual(result["live_authorization"]["order_gate"]["live_risk_profile"], A.CTC.LIVE_RISK_POLICY)
        self.assertEqual(result["live_authorization"]["order_gate"]["economics"]["cost_policy"]["entry_cost_multiple"], 1.1)
        self.assertEqual(result["live_authorization"]["order_gate"]["calibrated_probability"], .8)
        self.assertTrue(result["live_authorization"]["order_gate"]["source_gate"]["eligible"])
        self.assertIn(("orders", ACCOUNT), self.broker.reads)
        self.assertIn(("stops", ACCOUNT, "ACTIVE"), self.broker.reads)

    def test_factory_installs_real_loader_missing_evidence_is_explicit_and_observation_is_durable(self):
        result = self.authorize()
        self.assertFalse(result["eligible"])
        self.assertEqual(result["blockers"], ["LIVE_MODEL_EVIDENCE_REQUIRED", "LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED"])
        with self.connect() as c:
            row = c.execute(f"SELECT count(*) AS n FROM {A.OBSERVATION_TABLE}").fetchone()
        self.assertEqual(row["n"], 1)
        self.assertTrue(self.authority.status()["evidence_loader_configured"])

    def test_durable_store_failure_is_not_converted_to_permission(self):
        def offline():
            raise RuntimeError("do not expose database secret")
        evidence = A.LiveAdmissionEvidenceRepository(offline, ISSUER_KEY, clock=lambda: self.now)
        self.authority = A.create_live_admission(offline, self.broker, ACCOUNT, lambda: self.now, evidence=evidence)
        result = self.authorize()
        self.assertEqual(result["blockers"], ["LIVE_EVIDENCE_STORE_UNAVAILABLE"])
        self.assertNotIn("secret", json.dumps(result))

    def test_audited_evidence_is_byte_verified_and_idempotent_not_a_mark_verified_flag(self):
        doc, artifacts = self.model_document()
        receipt = self.publish(doc, artifacts)
        self.assertEqual(self.publish(doc, artifacts), receipt)
        self.assertFalse(receipt["trade_permission"])
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {A.EVIDENCE_TABLE}").fetchone()["n"], 1)
        broken = deepcopy(artifacts)
        broken[next(iter(broken))] = b"arbitrary forged evidence"
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_ARTIFACT_DIGEST_MISMATCH"):
            self.publish(doc, broken)

    def test_evidence_tampering_and_missing_raw_artifacts_are_detected_on_load(self):
        self.ready()
        with self.connect() as c:
            c.execute(f"UPDATE {A.ARTIFACT_TABLE} SET payload=%s WHERE sha256=%s",
                      (b"tamper", next(iter(self.model_document()[1]))))
        self.assertIn("LIVE_ARTIFACT_DIGEST_MISMATCH", self.authorize()["blockers"])

    def test_research_calibration_paper_flags_and_prior_are_not_live_evidence(self):
        self.terms.update(pwin=.99, calibrated_probability=.99, confidence=.99, paper_eligible=True)
        self.ready(model_change=lambda doc, _: doc["payload"]["calibration"].update(successes=59))
        self.assertIn("CALIBRATED_PROBABILITY_TOO_LOW", self.authorize()["blockers"])

    def test_small_oos_sample_blocks_even_with_passing_paper_permissions(self):
        self.ready(model_change=lambda doc, _: doc["payload"]["promotion"].update(oos_n=99))
        result = self.authorize()
        self.assertIn("OOS_SAMPLE_TOO_SMALL", result["blockers"])
        self.assertIn("MODEL_PROMOTION_REQUIRED", result["blockers"])

    def test_typed_promotion_and_cohort_evidence_reject_booleans_and_nan(self):
        for field, value in (("oos_n", True), ("oos_expectancy", float("nan")), ("code_ci_pass", "true")):
            with self.subTest(field=field):
                doc, blobs = self.model_document()
                doc["payload"]["promotion"][field] = value
                with self.assertRaises(A.AdmissionBlocked):
                    self.publish(doc, blobs)
        doc, blobs = self.model_document()
        doc["payload"]["calibration"]["successes"] = True
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_CALIBRATION_COUNTS_INVALID"):
            self.publish(doc, blobs)

    def test_old_deployment_or_source_evidence_cannot_promote_current_signal(self):
        doc, blobs = self.model_document()
        doc["binding"]["strategy_entry_sha"] = "2"*40
        doc["payload"]["calibration"]["population_binding_sha256"] = A._hash(doc["binding"])
        self.publish(doc, blobs)
        self.publish(*self.account_document())
        self.assertIn("LIVE_MODEL_EVIDENCE_REQUIRED", self.authorize()["blockers"])

    def test_latest_failed_review_does_not_fall_back_to_an_older_passing_model(self):
        self.ready()
        self.now += timedelta(seconds=1)
        doc, blobs = self.model_document()
        doc["payload"]["promotion"]["data_parity_pass"] = False
        self.publish(doc, blobs)
        self.assertIn("DATA_PARITY_NOT_PASSING", self.authorize()["blockers"])

    def test_same_timestamp_conflicting_reviews_block_as_ambiguous(self):
        self.ready()
        doc, blobs = self.model_document()
        doc["payload"]["promotion"]["data_parity_pass"] = False
        self.publish(doc, blobs)
        self.assertIn("LIVE_MODEL_EVIDENCE_AMBIGUOUS", self.authorize()["blockers"])

    def test_new_reviewed_revocation_is_terminal_for_that_evidence_binding(self):
        self.ready()
        self.now += timedelta(seconds=1)
        doc, blobs = self.model_document()
        doc["revoked"] = True
        self.publish(doc, blobs)
        self.assertIn("LIVE_MODEL_EVIDENCE_REVOKED", self.authorize()["blockers"])

    def test_model_expiry_is_enforced_while_broker_quote_is_still_fresh(self):
        self.ready(model_change=lambda doc, _: doc.update(valid_until=(self.now+timedelta(seconds=1)).isoformat()))
        self.now += timedelta(seconds=2)
        self.assertIn("LIVE_MODEL_EVIDENCE_STALE", self.authorize()["blockers"])

    def test_initial_status_exposes_live_switches_without_probe_or_false_missing_claim(self):
        with patch.dict("os.environ", {"VERITAS_LIVE_EXECUTION_ENABLED": "0", "VERITAS_LIVE_EXECUTION_ARMED": "0"}):
            status = self.authority.status()
        self.assertFalse(status["eligible"])
        self.assertIn("LIVE_MODEL_EVIDENCE_NOT_CHECKED", status["blockers"])
        self.assertIn("LIVE_EXECUTION_DISABLED", status["blockers"])
        self.assertIn("LIVE_EXECUTION_NOT_ARMED", status["blockers"])
        self.assertEqual(self.broker.reads, [])
        self.assertFalse(Path(self.path).exists())

    def test_absent_daily_weekly_cashflow_or_hwm_is_never_zero(self):
        cases = (("day_start_unit_nav", "LIVE_DAILY_NAV_BASELINE_REQUIRED"),
                 ("week_start_unit_nav", "LIVE_WEEKLY_NAV_BASELINE_REQUIRED"),
                 ("high_water_unit_nav", "LIVE_HIGH_WATER_NAV_REQUIRED"),
                 ("cashflows_reconciled", "LIVE_ACCOUNT_CASHFLOW_RECONCILIATION_REQUIRED"))
        for field, code in cases:
            with self.subTest(field=field):
                doc, blobs = self.account_document()
                doc["payload"].pop(field)
                with self.assertRaisesRegex(A.AdmissionBlocked, code):
                    self.publish(doc, blobs)

    def test_cashflow_adjusted_account_losses_enforce_all_current_stops(self):
        self.ready(account_change=lambda doc, _: doc["payload"].update(
            day_start_unit_nav="1.03", week_start_unit_nav="1.08", high_water_unit_nav="1.2"))
        blockers = self.authorize()["blockers"]
        for code in ("DAILY_LOSS_STOP", "WEEKLY_LOSS_STOP", "DRAWDOWN_LIMIT"):
            self.assertIn(code, blockers)

    def test_whole_account_kill_switch_is_preserved(self):
        self.ready(account_change=lambda doc, _: doc["payload"].update(kill_switch=True))
        self.assertIn("KILL_SWITCH_ACTIVE", self.authorize()["blockers"])

    def test_history_cannot_be_reused_after_account_equity_changes(self):
        self.ready()
        self.broker.equity -= 1
        self.assertIn("LIVE_ACCOUNT_HISTORY_SNAPSHOT_MISMATCH", self.authorize()["blockers"])

    def test_working_orders_elsewhere_in_account_cannot_be_ignored(self):
        self.broker.orders = [{"instrument_uid": OTHER, "lots_requested": 10000, "status": "NEW"}]
        self.assertEqual(self.authorize()["blockers"], ["LIVE_WHOLE_ACCOUNT_WORKING_ORDERS_UNRECONCILED"])

    def test_unmatched_stop_cannot_be_mistaken_for_protection(self):
        self.add_other_future()
        self.broker.holdings = []
        self.assertIn("LIVE_ACCOUNT_UNMATCHED_STOP_ORDERS", self.authorize()["blockers"])

    def test_unsupported_non_currency_and_fx_holdings_fail_closed(self):
        self.broker.holdings = [{"instrumentUid": OTHER, "kind": "options", "balance": "3", "blocked": "0"}]
        self.assertIn("LIVE_ACCOUNT_UNSUPPORTED_POSITION:"+OTHER, self.authorize()["blockers"])
        self.broker.holdings = []
        self.broker.cash.append(money(100, "usd"))
        self.assertIn("LIVE_ACCOUNT_FX_EXPOSURE_UNSUPPORTED:USD", self.authorize()["blockers"])

    def test_missing_protective_stop_or_partial_quantity_cannot_understate_other_risk(self):
        self.add_other_future()
        self.broker.stops[0]["lotsRequested"] = "2"
        self.assertIn("LIVE_ACCOUNT_PROTECTIVE_STOP_MISMATCH:"+OTHER, self.authorize()["blockers"])
        self.broker.stops = []
        self.assertIn("LIVE_ACCOUNT_PROTECTIVE_STOP_REQUIRED:"+OTHER, self.authorize()["blockers"])

    def test_other_future_uses_broker_lot_multiplier_and_full_correlation(self):
        self.add_other_future()
        self.ready(model_change=self.correlation)
        result = self.authorize()
        self.assertTrue(result["eligible"], result)
        self.assertAlmostEqual(result["account_risk"]["gross_after"], (24690+2000)/1000000)
        self.assertAlmostEqual(result["live_authorization"]["risk"]["by_asset"]["TBANK:"+OTHER], 200/1000000)
        self.assertEqual(len(result["live_authorization"]["risk"]["correlation_components"]), 1)

    def test_absent_or_partial_correlation_is_not_assumed_uncorrelated(self):
        self.add_other_future()
        self.ready()
        self.assertIn("LIVE_CORRELATION_EVIDENCE_REQUIRED", self.authorize()["blockers"])
        self.now += timedelta(seconds=1)
        doc, blobs = self.model_document()
        self.correlation(doc, blobs)
        doc["payload"]["correlations"]["matrix"] = {"CNYRUBF": {}}
        self.publish(doc, blobs)
        self.assertTrue(any(code.startswith("LIVE_CORRELATION_PAIR_REQUIRED:") for code in self.authorize()["blockers"]))

    def test_add_checks_entire_resulting_asset_exposure_against_single_asset_limit(self):
        account = replace(self.account, signed_lots=1, managed_signed_lots=1)
        held = deepcopy(self.held)
        held["target_price"] = "12.65"
        held["opened_at"] = (self.now-timedelta(days=2)).isoformat()
        self.terms = self.entry(account=account, held_terms=held)
        self.facts = TRADING.TradeFacts(self.spec, account, self.quote, held)
        self.broker.holdings = [{"instrumentUid": UID, "balance": "1", "blocked": "0"}]
        self.broker.equity = D("80000")
        self.ready()
        result = self.authorize()
        self.assertEqual(self.terms["lots"], 1)
        self.assertAlmostEqual(result["account_risk"]["fraction_nav_after"], 24690/80000)
        self.assertIn("SINGLE_ASSET_LIMIT", result["blockers"])
        self.assertGreater(result["live_authorization"]["order_gate"]["economics"]["modeled_funding_pct"], 0)

    def test_paper_eligible_flag_cannot_waive_live_rr_floor_after_geometry_changes(self):
        self.assertTrue(self.terms["economics"]["eligible"])
        self.terms["target_price"] = "12.42"
        self.ready()
        result = self.authorize()
        self.assertFalse(result["eligible"])
        self.assertIn("RR_BELOW_FINAL_FLOOR", result["blockers"])
        self.assertIn("NET_REWARD_RISK_BELOW_FLOOR", result["blockers"])

    def test_costs_also_consume_unchanged_live_stop_risk_caps(self):
        self.broker.equity = D("10000")
        self.ready()
        result = self.authorize()
        self.assertIn("STOP_RISK_LIMIT", result["blockers"])
        self.assertGreater(result["account_risk"]["net_stop_risk_nav"], result["live_authorization"]["risk"]["proposed_stop_risk_nav"])

    def test_disabled_or_unarmed_runtime_cannot_be_overridden_by_audit(self):
        self.ready()
        with patch.dict("os.environ", {"VERITAS_LIVE_EXECUTION_ARMED": "0"}):
            self.assertIn("LIVE_EXECUTION_NOT_ARMED", self.authorize()["blockers"])

    def test_position_movement_and_slow_io_require_fresh_admission(self):
        def change_position(raw):
            raw["futures"].append({"instrumentUid": UID, "balance": "1", "blocked": "0"})
        self.broker.after_second_positions = change_position
        self.assertIn("LIVE_ACCOUNT_CHANGED_DURING_ADMISSION", self.authorize()["blockers"])
        def advance(_):
            self.now += timedelta(seconds=16)
        self.broker.after_second_positions = advance
        self.assertIn("BROKER_QUOTE_STALE", self.authorize()["blockers"])

    def test_cached_status_never_performs_io_and_pass_expires_with_quote(self):
        self.ready()
        self.assertTrue(self.authorize()["eligible"])
        count = len(self.broker.reads)
        self.now += timedelta(seconds=16)
        status = self.authority.status()
        self.assertFalse(status["eligible"])
        self.assertEqual(status["blockers"], ["LIVE_ADMISSION_RECHECK_REQUIRED"])
        self.assertEqual(len(self.broker.reads), count)

    def test_existing_validation_snapshot_intake_requires_actual_export_schema(self):
        with self.connect() as c:
            c.execute("CREATE TABLE validation_snapshots(snapshot_id INTEGER PRIMARY KEY,payload JSONB NOT NULL)")
            c.execute("INSERT INTO validation_snapshots VALUES(%s,%s)", (1, json.dumps({"paper_eligible": True, "confidence": .99})))
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_VALIDATION_ARTIFACT_EXPORT_REQUIRED"):
            A.publish_validation_snapshot(self.connect, 1, {}, {}, now=self.now)
        doc, artifacts = self.model_document()
        with self.connect() as c:
            c.execute("INSERT INTO validation_snapshots VALUES(%s,%s)", (2, json.dumps({"live_authority_artifact": doc})))
        receipt = A.publish_validation_snapshot(self.connect, 2, artifacts, {
            "reviewer": "independent-test-auditor", "audit_id": "review-2", "reviewed_at": self.now.isoformat(),
            "decision": "ACCEPT_EVIDENCE"}, now=self.now)
        self.assertFalse(receipt["trade_permission"])
        self.assertEqual(receipt["kind"], "MODEL")

    def signed_payload(self, kind="MODEL"):
        doc, artifacts = self.model_document() if kind == "MODEL" else self.account_document()
        review = {"reviewer": "offline-independent-reviewer", "audit_id": "synthetic-signed-audit",
                  "reviewed_at": self.now.isoformat(), "decision": "ACCEPT_EVIDENCE"}
        return A.audited_evidence_payload(self.terms, doc, artifacts, review)

    def sign(self, payload, key=ISSUER_KEY):
        return hmac.new(key, A.canonical_json(payload).encode(), hashlib.sha256).hexdigest()

    def test_operator_import_requires_independent_issuer_signature_before_any_storage(self):
        payload = self.signed_payload()
        for signature in (None, "0"*64, self.sign(payload, b"different-independent-key"*2)):
            with self.subTest(signature=str(signature)[:8]):
                with self.assertRaisesRegex(A.LiveAdmissionError, "LIVE_EVIDENCE_SIGNATURE_INVALID"):
                    self.evidence.publish(payload, signature)
        self.assertFalse(Path(self.path).exists())
        self.assertFalse(self.evidence.status()["initialized"])

    def test_missing_issuer_is_an_explicit_inert_configuration_blocker(self):
        self.authority = A.create_live_admission(self.connect, self.broker, ACCOUNT, lambda: self.now)
        self.assertFalse(self.authority.status()["evidence"]["configured"])
        result = self.authorize()
        self.assertEqual(result["blockers"], ["LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED"])
        self.assertFalse(result["evidence_request"]["trade_permission"])
        self.assertEqual(result["evidence_request"]["terms"], self.terms)
        self.assertEqual(self.broker.reads, [])
        self.assertFalse(Path(self.path).exists())

    def test_unsigned_internal_audit_archives_cannot_bypass_issuer_intake(self):
        for make in (self.model_document, self.account_document):
            doc, artifacts = make()
            A.publish_audited_artifact(self.connect, doc, artifacts, {
                "reviewer": "offline-reviewer", "audit_id": "unsigned-archive",
                "reviewed_at": self.now.isoformat(), "decision": "ACCEPT_EVIDENCE"}, now=self.now)
        self.assertEqual(self.authorize()["blockers"],
                         ["LIVE_MODEL_EVIDENCE_REQUIRED", "LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED"])

    def test_signed_import_idempotency_does_not_allow_id_reuse_for_different_metrics(self):
        payload = self.signed_payload()
        receipt = self.evidence.publish(payload, self.sign(payload))
        self.assertEqual(receipt, self.evidence.publish(deepcopy(payload), self.sign(payload)))
        doc = json.loads(payload["document_json"])
        doc["payload"]["calibration"]["successes"] = 90
        payload["document_json"] = A._json(doc)
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_EVIDENCE_ID_REUSED"):
            self.evidence.publish(payload, self.sign(payload))
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {A.TABLE}").fetchone()["n"], 1)

    def test_database_edit_and_recomputed_hash_cannot_forge_issuer_proof(self):
        self.ready()
        with self.connect() as c:
            row = c.execute(f"SELECT evidence_id,payload FROM {A.TABLE} WHERE kind=%s", ("MODEL_ADMISSION",)).fetchone()
            payload = json.loads(row["payload"])
            doc = json.loads(payload["document_json"])
            doc["payload"]["calibration"]["successes"] = 99
            payload["document_json"] = A._json(doc)
            c.execute(f"UPDATE {A.TABLE} SET payload=%s,payload_hash=%s WHERE evidence_id=%s",
                      (A.canonical_json(payload), A._hash(payload), row["evidence_id"]))
        self.assertIn("LIVE_EVIDENCE_SIGNATURE_INVALID", self.authorize()["blockers"])

    def test_rotating_issuer_key_invalidates_old_stored_certificates(self):
        self.ready()
        self.evidence._key = b"a-new-independent-issuer-key-value"*2
        result = self.authorize()
        self.assertFalse(result["eligible"])
        self.assertIn("LIVE_EVIDENCE_SIGNATURE_INVALID", result["blockers"])

    def test_exact_order_quantity_and_source_and_version_cannot_reuse_signed_scope(self):
        self.ready()
        original = deepcopy(self.terms)
        for field, value in (("lots", 1), ("model_version", "other-model"), ("horizon", "1h"),
                             ("prepared_at", (self.now+timedelta(microseconds=1)).isoformat())):
            with self.subTest(field=field):
                self.terms = dict(original, **{field: value})
                self.assertIn("LIVE_MODEL_EVIDENCE_REQUIRED", self.authorize()["blockers"])
        self.terms = original
        self.assertTrue(self.authorize()["eligible"])
        self.terms = dict(original, source_identity={"source": "wrong-unverified-feed"})
        self.assertIn("LIVE_SIGNAL_SOURCE_IDENTITY_REQUIRED", self.authorize()["blockers"])

    def test_signed_deadline_bounds_real_verdict_and_cannot_be_extended_by_document_expiry(self):
        payload = self.signed_payload()
        deadline = self.now+timedelta(seconds=1)
        payload["valid_until"] = deadline.isoformat()
        self.evidence.publish(payload, self.sign(payload))
        self.publish(*self.account_document())
        result = self.authorize()
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["valid_until"], deadline.isoformat())
        self.now += timedelta(seconds=2)
        self.assertIn("LIVE_MODEL_EVIDENCE_STALE", self.authorize()["blockers"])

    def test_prior_data_only_signature_does_not_invent_artifact_bytes_or_nav_baselines(self):
        # The concurrently implemented v1 issuer format authenticated assertions
        # but did not contain original artifacts, cohort counts or unit NAV.
        payload = {"version": "currency-live-admission-v1", "kind": "MODEL_ADMISSION",
                   "data": {"calibrated_probability": "0.99", "daily_pnl_pct": "0"}}
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_AUDITED_ARTIFACT_SCHEMA_REQUIRED"):
            self.evidence.publish(payload, self.sign(payload))
        self.assertFalse(Path(self.path).exists())

    def test_same_artifact_cannot_be_claimed_as_all_independent_evidence_domains(self):
        payload = self.signed_payload()
        doc = json.loads(payload["document_json"])
        digest = next(iter(payload["artifact_bytes"]))
        for entry in doc["artifacts"].values():
            entry["sha256"] = digest
        payload["document_json"] = A._json(doc)
        payload["artifact_bytes"] = {digest: payload["artifact_bytes"][digest]}
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_DISTINCT_INDEPENDENT_ARTIFACTS_REQUIRED"):
            self.evidence.publish(payload, self.sign(payload))

    def test_signed_staged_large_originals_fit_transport_and_are_rechecked_at_runtime(self):
        doc, artifacts = self.model_document()
        large_original = b"OFFLINE SYNTHETIC ORIGINAL AUDIT RECORD\n" * 4000
        original_digest = doc["artifacts"]["oos"]["sha256"]
        digest = hashlib.sha256(large_original).hexdigest()
        doc["artifacts"]["oos"]["sha256"] = digest
        artifacts.pop(original_digest)
        artifacts[digest] = large_original
        review = {"reviewer": "offline-independent-reviewer", "audit_id": "staged-original-review",
                  "reviewed_at": self.now.isoformat(), "decision": "ACCEPT_EVIDENCE"}
        with self.assertRaisesRegex(A.AdmissionBlocked, "EVIDENCE_TOO_LARGE"):
            A.audited_evidence_payload(self.terms, doc, artifacts, review)
        A.publish_audited_artifact(self.connect, doc, artifacts, review, now=self.now)
        payload = A.audited_evidence_payload(self.terms, doc, None, review)
        self.assertTrue(all(value is None for value in payload["artifact_bytes"].values()))
        self.evidence.publish(payload, self.sign(payload))
        self.publish(*self.account_document())
        self.assertTrue(self.authorize()["eligible"])
        with self.connect() as c:
            c.execute(f"UPDATE {A.ARTIFACT_TABLE} SET payload=%s WHERE sha256=%s", (b"forged blob", digest))
        self.assertIn("LIVE_ARTIFACT_DIGEST_MISMATCH", self.authorize()["blockers"])

    def test_signed_references_without_actual_staged_bytes_cannot_import(self):
        self.evidence.initialize()
        payload = self.signed_payload()
        payload["artifact_bytes"] = dict.fromkeys(payload["artifact_bytes"])
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_ARTIFACT_BYTES_REQUIRED"):
            self.evidence.publish(payload, self.sign(payload))
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {A.TABLE}").fetchone()["n"], 0)

    def test_signed_document_transport_is_bounded_and_finite_metrics_stay_exact(self):
        payload = self.signed_payload()
        self.assertIsInstance(payload["document_json"], str)
        self.assertEqual(json.loads(payload["document_json"])["payload"]["promotion"]["oos_expectancy"], .25)
        for changed in (dict(payload, unexpected=1.5), dict(payload, issuer_id="x"*(A.MAX_BYTES+1))):
            with self.assertRaises(A.AdmissionBlocked):
                A.canonical_json(changed)
        doc = json.loads(payload["document_json"])
        doc["payload"].pop("calibration")
        payload["document_json"] = A._json(doc)
        with self.assertRaisesRegex(A.AdmissionBlocked, "LIVE_CALIBRATION_MODEL_REQUIRED"):
            self.evidence.publish(payload, self.sign(payload))

    def test_retained_incoming_constructor_uses_full_strict_authority(self):
        self.ready()
        authority = A.CurrencyLiveAdmission(adapter=self.broker, evidence=self.evidence, account_id=ACCOUNT,
                                            instrument_uid=UID, environment="production", clock=lambda: self.now)
        result = authority(terms=self.terms, facts=self.facts, now=self.now)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["account_equity_rub"], "1000000")
        self.assertTrue(authority.status()["evidence"]["configured"])
        wrong = dict(self.terms, instrument_uid=OTHER)
        self.assertEqual(authority(terms=wrong, facts=self.facts, now=self.now)["blockers"], ["LIVE_ACCOUNT_SCOPE_MISMATCH"])

    def test_later_failed_review_for_another_scope_cannot_resurrect_prior_passing_review(self):
        self.ready()
        original = deepcopy(self.terms)
        self.now += timedelta(seconds=1)
        self.terms["lots"] = 1
        doc, artifacts = self.model_document()
        doc["payload"]["promotion"]["data_parity_pass"] = False
        self.publish(doc, artifacts)
        self.terms = original
        self.assertIn("LIVE_MODEL_ISSUER_REATTESTATION_REQUIRED", self.authorize()["blockers"])

    def test_current_structural_proof_keeps_paper_ladder_out_of_actual_live_economics(self):
        from test_veritas_currency_structural_trading import native_fixture
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                row, admission, self.spec, account, self.quote, self.now = native_fixture(direction=direction)
                self.account = replace(account, account_id=ACCOUNT)
                self.terms = P.prepare_entry(row, admission, self.spec, self.account, self.quote, now=self.now)
                self.facts = TRADING.TradeFacts(self.spec, self.account, self.quote)
                self.broker = Broker(self.spec, self.quote, self.account)
                self.authority = A.create_live_admission(self.connect, self.broker, ACCOUNT, lambda: self.now,
                                                        evidence=self.evidence)
                self.assertEqual(len(P.approved_entry_context(self.terms)["event"]["target_ladder"]), 2)
                self.ready()
                result = self.authorize()
                self.assertTrue(result["eligible"], result)
                economics = result["live_authorization"]["order_gate"]["economics"]
                self.assertEqual(economics["target_ladder"], [])
                self.assertGreater(economics["minimum_reward_risk"], 0)
                self.assertEqual(economics["economics_policy"]["execution_mode"], "LIVE")
                self.assertEqual(economics["cost_policy"]["entry_cost_multiple"], 1.1)


if __name__ == "__main__":
    unittest.main()
