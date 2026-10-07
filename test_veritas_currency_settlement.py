"""Synthetic broker evidence only; PostgreSQL uses the guarded ephemeral DSN."""
import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock

import veritas_currency_settlement as S
import veritas_currency_trade_ledger as L
import veritas_tbank_trading as T
import test_veritas_currency_trade_ledger as LT
import test_veritas_tbank_trading as TT

D, NOW, SPEC = Decimal, LT.NOW, LT.SPEC
ACCOUNT, UID = "test-account", SPEC.instrument_uid


def fixture(*, fills=None, observed_at=None, funding="2", fee="2", revision=1, statement=True):
    fills = fills or [LT.fill("one", "BUY", 1, "12.00", fee="9")]
    at = observed_at or NOW + timedelta(seconds=5)
    through = at - timedelta(seconds=1)
    rows = []
    fee_rows = []
    for index, fill in enumerate(fills):
        oid = "operation-trade-"+str(index)
        rows.append({"id":oid,"brokerAccountId":ACCOUNT,"instrumentUid":UID,
            "date":fill["executed_at"].isoformat(), "state":"OPERATION_STATE_EXECUTED",
            "type":"OPERATION_TYPE_"+fill["side"], "payment":TT.money("0"),
            "tradesInfo":{"trades":[{"num":fill["trade_id"],"quantity":str(fill["lots"]),
                "date":fill["executed_at"].isoformat(),"price":TT.money(str(fill["price"]))}]}})
        rows.append({"id":"fee-"+str(index),"brokerAccountId":ACCOUNT,"parentOperationId":oid,
            "date":through.isoformat(),"state":"OPERATION_STATE_EXECUTED",
            "type":"OPERATION_TYPE_BROKER_FEE","payment":TT.money(str(-D(fee)))})
        fee_rows.append({"trade_id":fill["trade_id"],"broker_order_id":fill["broker_order_id"],
            "quantity_lots":fill["lots"],"broker_commission_rub":D(fee)-D("1"),
            "exchange_commission_rub":D("0.5"),"clearing_commission_rub":D("0.5")})
    projected = L.project(fills, [], [], SPEC)
    mark = D("12.10") if projected["signed_lots"] else None
    economic = projected["realized_pnl_rub"]
    if mark is not None:
        economic += (mark-projected["average_entry_price"])*projected["signed_lots"]*SPEC.rub_per_price_unit_per_lot
    vm_cash = economic-D("3")
    for oid, kind, amount in (("vm-1","ACCRUING_VARMARGIN" if vm_cash >= 0 else "WRITING_OFF_VARMARGIN",vm_cash),
                              ("fund-1","FUNDING",-D(funding))):
        rows.append({"id":oid,"brokerAccountId":ACCOUNT,"instrumentUid":UID,
            "date":through.isoformat(),"state":"OPERATION_STATE_EXECUTED",
            "type":"OPERATION_TYPE_"+kind,"payment":TT.money(str(amount))})
    snapshot = {"source":"TBANK_OPERATIONS_BY_CURSOR","account_id":ACCOUNT,"instrument_uid":UID,
        "from":NOW.isoformat(),"to":through.isoformat(),"received_at":at.isoformat(),
        "retrieval_complete":True,"finality_proven":False,"operations":rows,"pages":1}
    report = {"version":S.REPORT_VERSION,"source":"BROKER_SETTLEMENT_STATEMENT",
        "account_id":ACCOUNT,"instrument_uid":UID,"ticker":"CNYRUBF","currency":"RUB","status":"FINAL",
        "statement_id":"settlement-document","revision":revision,"artifact_sha256":str(revision)*64,
        "period_from":NOW.isoformat(),"settled_through":through.isoformat(),"issued_at":through.isoformat(),
        "cash_operation_ids":sorted(["vm-1","fund-1"]+["fee-"+str(i) for i in range(len(fills))]),
        "variation_margin_cash_rub":vm_cash,"explicit_funding_rub":D(funding),"embedded_funding_rub":D("3"),
        "fee_cash_rub":D(fee)*len(fills),"other_fees_rub":D("0"),"closing_signed_lots":projected["signed_lots"],
        "closing_settlement_price_points":mark,"trade_fees":fee_rows}
    return fills, {"operations":snapshot,"statement":report if statement else None}, at


def assess(observation, fills, now, **extra):
    return S.assess(observation, account=ACCOUNT, uid=UID, bound_at=NOW,
                    fills=fills, spec=SPEC, now=now, **extra)


class SettlementEvidenceTests(unittest.TestCase):
    def test_cursor_exhaustion_and_elapsed_time_never_establish_finality(self):
        fills, observation, now = fixture(statement=False)
        for age in (0, 86400):
            result = assess(observation, fills, now+timedelta(seconds=age))
            self.assertEqual(result["funding_rub"],D("2"))
            self.assertFalse(result["funding_reconciled"])
            self.assertFalse(result["finality_proven"])
            self.assertIn("BROKER_SETTLEMENT_STATEMENT_REQUIRED",result["reasons"])

    def test_numeric_report_closes_economic_bridge_without_counting_vm_twice(self):
        fills, observation, now = fixture()
        evidence = assess(observation, fills, now)
        self.assertTrue(evidence["funding_reconciled"])
        projected = L.project(fills, [], [], SPEC, settlement=evidence)
        self.assertEqual(projected["funding_rub"],D("5"))
        self.assertEqual(projected["fees_rub"],D("2"))
        self.assertEqual(L.valuation(LT.state(projected),D("12.10"),SPEC)["currency_nav_rub"],D("10093"))

    def test_changed_operation_ids_and_signed_funding_corrections_replace_observation(self):
        fills, observation, now = fixture(statement=False)
        first = assess(observation, fills, now)
        renamed = copy.deepcopy(observation)
        renamed["operations"]["operations"][-1]["id"] = "broker-reissued-operation-id"
        second = assess(renamed, fills, now, previous=first)
        self.assertEqual(second["funding_rub"],first["funding_rub"])
        renamed["operations"]["operations"][-1]["payment"] = TT.money("1")
        corrected = assess(renamed, fills, now, previous=second)
        self.assertEqual(corrected["funding_rub"],D("-1"))
        renamed["operations"]["operations"][-1]["state"] = "OPERATION_STATE_CANCELED"
        self.assertEqual(assess(renamed,fills,now,previous=corrected)["funding_rub"],D("0"))

    def test_account_instrument_interval_and_unattributed_costs_are_not_inferred(self):
        fills, observation, now = fixture()
        other_account = copy.deepcopy(observation)
        other_account["operations"]["operations"][0]["brokerAccountId"] = "foreign-account"
        with self.assertRaisesRegex(S.SettlementError,"OPERATION_ACCOUNT_MISMATCH"):
            assess(other_account,fills,now)
        foreign = copy.deepcopy(observation)
        foreign["operations"]["instrument_uid"] = "foreign-instrument"
        with self.assertRaisesRegex(S.SettlementError,"OBSERVATION_SCOPE_MISMATCH"):
            assess(foreign,fills,now)
        unallocated = copy.deepcopy(observation)
        unallocated["operations"]["operations"].append({"id":"account-service-charge","brokerAccountId":ACCOUNT,
            "date":now.isoformat(),"type":"OPERATION_TYPE_SERVICE_FEE","state":"OPERATION_STATE_EXECUTED",
            "payment":TT.money("-10")})
        unallocated["operations"]["operations"][-1]["date"] = observation["operations"]["to"]
        result = assess(unallocated,fills,now)
        self.assertFalse(result["funding_reconciled"])
        self.assertIn("BROKER_COST_ALLOCATION_UNRESOLVED",result["reasons"])

    def test_parent_vm_requires_complete_exact_child_attribution(self):
        fills, observation, now = fixture()
        vm = observation["operations"]["operations"][-2]
        vm.pop("instrumentUid")
        vm["payment"] = TT.money("107")
        vm["childOperations"] = [{"instrumentUid":UID,"payment":TT.money("97")},
                                 {"instrumentUid":"foreign-uid","payment":TT.money("10")}]
        self.assertTrue(assess(observation,fills,now)["funding_reconciled"])
        vm["childOperations"][0]["payment"] = TT.money("98")
        with self.assertRaisesRegex(S.SettlementError,"CHILD_TOTAL_MISMATCH"):
            assess(observation,fills,now)

    def test_pending_missing_fill_fee_component_and_invented_bridge_stay_blocked(self):
        fills, base, now = fixture()
        for mutate, reason in (
            (lambda x:x["operations"]["operations"][0].update(state="OPERATION_STATE_PROGRESS"),"BROKER_OPERATIONS_PENDING"),
            (lambda x:x["operations"]["operations"][0].update(tradesInfo={"trades":[]}),"BROKER_EXECUTIONS_NOT_FULLY_OBSERVED"),
            (lambda x:x["statement"].update(embedded_funding_rub=D("100")),"BROKER_SETTLEMENT_ECONOMIC_BRIDGE_MISMATCH"),
            (lambda x:x["statement"]["trade_fees"][0].pop("clearing_commission_rub"),"EXACT_DECIMAL_REQUIRED"),
            (lambda x:x["statement"].update(settled_through=NOW.isoformat()),"BROKER_SETTLEMENT_OPEN_EXPOSURE_AFTER_COVERAGE"),
        ):
            with self.subTest(reason=reason):
                observation=copy.deepcopy(base)
                mutate(observation)
                result=assess(observation,fills,now)
                self.assertFalse(result["funding_reconciled"])
                self.assertIn(reason,result["reasons"])

    def test_report_fee_correction_downwards_replaces_prior_actual_fees(self):
        fills, observation, now = fixture(fee="1.5",revision=2)
        result=assess(observation,fills,now)
        self.assertTrue(result["costs_reconciled"])
        fee_observation={"client_order_id":fills[0]["client_order_id"],"cumulative_fee_rub":D("9"),"filled_lots":1}
        projected=L.project(fills,[fee_observation],[],SPEC,settlement=result)
        self.assertEqual(projected["fees_rub"],D("1.5"))
        self.assertEqual(projected["funding_rub"],D("5"))
        self.assertFalse(assess(observation,fills,now,legacy_funding=True)["funding_reconciled"])

    def test_margin_and_other_fees_require_full_cash_coverage_without_becoming_funding(self):
        for kind in (14, 66, "OPERATION_TYPE_MARGIN_FEE", "OPERATION_TYPE_OTHER_FEE"):
            for child_allocation in (False, True):
                with self.subTest(kind=kind, child_allocation=child_allocation):
                    fills, observation, now=fixture()
                    row={"id":"separate-cost", "brokerAccountId":ACCOUNT, "instrumentUid":UID,
                         "date":observation["operations"]["to"], "state":"OPERATION_STATE_EXECUTED",
                         "type":kind, "payment":TT.money("-2")}
                    if child_allocation:
                        row.update(instrumentUid="",payment=TT.money("0"),childOperations=[
                            {"instrumentUid":UID,"payment":TT.money("-2")},
                            {"instrumentUid":"foreign","payment":TT.money("2")}])
                    observation["operations"]["operations"].append(row)
                    uncovered=assess(observation,fills,now)
                    self.assertFalse(uncovered["funding_reconciled"])
                    observation["statement"]["cash_operation_ids"].append("separate-cost")
                    observation["statement"]["cash_operation_ids"].sort()
                    observation["statement"].update(other_fees_rub="2",fee_cash_rub="4")
                    covered=assess(observation,fills,now)
                    self.assertTrue(covered["funding_reconciled"], covered["reasons"])
                    projected=L.project(fills,[],[],SPEC,settlement=covered)
                    self.assertEqual(projected["fees_rub"],D("4"))
                    self.assertEqual(projected["funding_rub"],D("5"))
                    row["state"]="OPERATION_STATE_PROGRESS"
                    pending=assess(observation,fills,now)
                    self.assertFalse(pending["funding_reconciled"])
                    self.assertIn("BROKER_COST_OPERATION_PENDING",pending["reasons"])

    def test_historical_final_flat_report_is_reusable_only_with_fresh_full_cash_match(self):
        fills=[LT.fill("one","BUY",1,"12.00",fee="2"),
               LT.fill("close","SELL",1,"12.10",fee="2",offset=1,metadata=LT.terms("CLOSE"))]
        fills,observation,now=fixture(fills=fills)
        through=observation["statement"]["settled_through"]
        later=now+timedelta(days=1)
        observation["operations"].update(to=(later-timedelta(seconds=1)).isoformat(),received_at=later.isoformat())
        result=assess(observation,fills,later)
        self.assertTrue(result["funding_reconciled"])
        self.assertTrue(result["flat_after_settlement"])
        self.assertEqual(result["settlement_through"],L.utc(through))
        observation["operations"]["operations"][-1]["payment"]=TT.money("-4")
        changed=assess(observation,fills,later)
        self.assertFalse(changed["funding_reconciled"])
        self.assertIn("BROKER_SETTLEMENT_CASH_TOTAL_MISMATCH",changed["reasons"])

    def test_disabled_collection_has_no_database_or_broker_side_effects(self):
        ledger, broker=Mock(enabled=False),Mock()
        reconciler=S.CurrencySettlementReconciler(ledger,broker)
        self.assertEqual(reconciler.collect(ACCOUNT,UID),{"disabled":True})
        ledger._run.assert_not_called()
        broker.get_operations.assert_not_called()


class ScopedBrokerOperationsTests(unittest.TestCase):
    def setUp(self):
        self.transport=TT.FakeTransport()
        config=T.ExecutionConfig(allowed_account_ids=frozenset({TT.ACCOUNT}),allowed_instrument_uids=frozenset({TT.UID}))
        self.adapter=T.TBankTradingAdapter(TT.TOKEN,transport=self.transport,config=config)

    def read(self,**kwargs):
        return self.adapter.get_operations(TT.ACCOUNT,TT.UID,from_time="2026-10-06T00:00:00Z",to_time=TT.STAMP,**kwargs)

    def row(self,oid):
        return {"id":oid,"brokerAccountId":TT.ACCOUNT,"type":"OPERATION_TYPE_FUNDING","payment":TT.money("-1")}

    def test_all_pages_include_account_costs_and_cannot_claim_finality(self):
        def respond(body):
            return TT.Response({"items":[self.row("funding-2")],"hasNext":False}) if body.get("cursor") else TT.Response({
                "items":[self.row("funding-1")],"hasNext":True,"nextCursor":"second"})
        self.transport.handlers["GetOperationsByCursor"]=respond
        result=self.read()
        self.assertEqual(len(result["operations"]),2)
        self.assertTrue(result["retrieval_complete"])
        self.assertFalse(result["finality_proven"])
        for call in self.transport.calls:
            self.assertEqual(call["name"],"GetOperationsByCursor")
            self.assertNotIn("instrumentId",call["body"])
            for flag in ("withoutCommissions","withoutTrades","withoutOvernights"):
                self.assertIs(call["body"][flag],False)

    def test_stalled_truncated_foreign_and_changing_pages_fail_closed(self):
        self.transport.handlers["GetOperationsByCursor"]=TT.Response({"items":[self.row("x")],"hasNext":True,"nextCursor":"same"})
        with self.assertRaisesRegex(T.TradingError,"PAGINATION_STALLED"):
            self.read()
        with self.assertRaisesRegex(T.TradingError,"PAGINATION_INCOMPLETE"):
            self.read(max_pages=1)
        self.transport.handlers["GetOperationsByCursor"]=TT.Response({"items":[dict(self.row("x"),brokerAccountId="another")],"hasNext":False})
        with self.assertRaisesRegex(T.TradingError,"ACCOUNT_MISMATCH"):
            self.read()
        self.transport.handlers["GetOperationsByCursor"]=lambda body:TT.Response({"items":[dict(self.row("x"),payment=TT.money("-2" if body.get("cursor") else "-1"))],"hasNext":not bool(body.get("cursor")),"nextCursor":"p2"})
        with self.assertRaisesRegex(T.TradingError,"CHANGED_DURING_PAGINATION"):
            self.read()

    def test_scoping_and_report_sandbox_rejection_precede_network(self):
        with self.assertRaisesRegex(T.TradingError,"ACCOUNT_OUTSIDE"):
            self.adapter.get_operations("another",TT.UID,from_time=TT.STAMP,to_time=TT.STAMP)
        with self.assertRaisesRegex(T.TradingError,"PAGE_SIZE"):
            self.read(page_size=2)
        self.adapter.config=replace(self.adapter.config,environment="sandbox")
        with self.assertRaisesRegex(T.TradingError,"UNAVAILABLE_IN_SANDBOX"):
            self.adapter.get_broker_report(TT.ACCOUNT,TT.UID,from_time="2026-10-06T00:00:00Z",to_time=TT.STAMP)
        self.assertEqual(self.transport.calls,[])


@unittest.skipUnless(LT.TEST_DSN,"explicit ephemeral PostgreSQL DSN not configured")
class SettlementPostgresTests(unittest.TestCase):
    setUp=LT.CurrencyLedgerPostgresTests.setUp
    drop_schema=LT.CurrencyLedgerPostgresTests.drop_schema
    posting=LT.CurrencyLedgerPostgresTests.posting
    record=LT.CurrencyLedgerPostgresTests.record
    snapshot=LT.CurrencyLedgerPostgresTests.snapshot
    reconcile=LT.CurrencyLedgerPostgresTests.reconcile

    def service(self):
        return S.CurrencySettlementReconciler(self.ledger,Mock(),clock=lambda:self.now)

    def test_restart_idempotency_downward_corrections_and_new_fill_invalidate_coverage(self):
        self.record(fee="9")
        self.now=NOW+timedelta(seconds=5)
        fills,observation,_=fixture(observed_at=self.now)
        result=self.service().reconcile(ACCOUNT,UID,observation=observation)
        self.assertTrue(result["funding_reconciled"])
        self.reconcile(1,"position-proof")
        before=self.snapshot("12.10")
        self.assertTrue(before["entries_allowed"])
        self.assertEqual(before["currency_nav_rub"],D("10093"))
        self.ledger=L.CurrencyTradeLedger(self.connect,enabled=True,clock=lambda:self.now)
        self.service().reconcile(ACCOUNT,UID,observation=observation)
        self.assertEqual(self.snapshot("12.10")["ledger_revision"],before["ledger_revision"])
        self.now+=timedelta(seconds=1)
        _,correction,_=fixture(observed_at=self.now,funding="1",fee="1.5",revision=2)
        self.service().reconcile(ACCOUNT,UID,observation=correction)
        corrected=self.snapshot("12.10")
        self.assertEqual(corrected["fees_rub"],D("1.5"))
        self.assertEqual(corrected["funding_rub"],D("4"))
        self.assertEqual(corrected["currency_nav_rub"],D("10094.5"))
        self.assertFalse(corrected["reconciled"])
        self.record(tid="new",metadata=LT.terms("ADD"),fee="1")
        self.assertFalse(self.snapshot("12.10")["funding_reconciled"])
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {L.SETTLEMENTS}").fetchone()["n"],2)
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {L.FUNDING}").fetchone()["n"],0)

    def test_revision_conflict_and_transaction_rollback_cannot_rewrite_settlement(self):
        self.record(fee="9")
        self.now=NOW+timedelta(seconds=5)
        _,observation,_=fixture(observed_at=self.now)
        with self.assertRaisesRegex(RuntimeError,"synthetic rollback"):
            with self.connect() as c,c.transaction():
                self.service().reconcile_on(c,ACCOUNT,UID,observation=observation)
                raise RuntimeError("synthetic rollback")
        self.assertEqual(self.snapshot("12.10")["funding_rub"],D("0"))
        self.assertFalse(self.snapshot("12.10")["funding_reconciled"])
        self.service().reconcile(ACCOUNT,UID,observation=observation)
        _,changed,_=fixture(observed_at=self.now,funding="1",revision=1)
        with self.assertRaisesRegex(S.SettlementError,"REVISION_CONFLICT"):
            self.service().reconcile(ACCOUNT,UID,observation=changed)
        self.assertEqual(self.snapshot("12.10")["funding_rub"],D("5"))

    def test_actual_funding_persists_without_false_finality_and_hwm_is_held(self):
        self.record(fee="9")
        self.now=NOW+timedelta(seconds=5)
        _,observation,_=fixture(observed_at=self.now,statement=False)
        result=self.service().reconcile(ACCOUNT,UID,observation=observation)
        self.assertFalse(result["funding_reconciled"])
        current=self.snapshot("12.10")
        self.assertEqual(current["funding_rub"],D("2"))
        self.assertEqual(current["high_water_rub"],D("10000"))
        self.assertFalse(self.reconcile(1)["entries_allowed"])

    def test_historical_flat_statement_is_reloaded_and_revalidated_after_restart(self):
        self.record(fee="2")
        self.now=NOW+timedelta(seconds=1)
        self.record(tid="close",side="SELL",price="12.10",fee="2",metadata=LT.terms("CLOSE"))
        self.now=NOW+timedelta(seconds=5)
        fills=[LT.fill("one","BUY",1,"12.00",fee="2"),
               LT.fill("close","SELL",1,"12.10",fee="2",offset=1,metadata=LT.terms("CLOSE"))]
        _,observation,_=fixture(fills=fills,observed_at=self.now)
        self.service().reconcile(ACCOUNT,UID,observation=observation)
        self.now+=timedelta(days=1)
        self.ledger=L.CurrencyTradeLedger(self.connect,enabled=True,clock=lambda:self.now)
        snapshot=copy.deepcopy(observation["operations"])
        snapshot.update(to=self.now.isoformat(),received_at=self.now.isoformat())
        broker=Mock()
        broker.get_operations.return_value=snapshot
        restarted=S.CurrencySettlementReconciler(self.ledger,broker,clock=lambda:self.now)
        collected=restarted.collect(ACCOUNT,UID)
        self.assertIsNotNone(collected["statement"])
        result=restarted.reconcile(ACCOUNT,UID,observation=collected)
        self.assertTrue(result["funding_reconciled"])
        self.reconcile(0,"fresh-flat-proof")
        self.assertTrue(self.snapshot("12.10")["entries_allowed"])


if __name__ == "__main__":
    unittest.main()
