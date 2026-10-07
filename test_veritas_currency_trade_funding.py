"""Synthetic public contracts; opt-in PostgreSQL only in a disposable database."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import Event, Lock
import unittest
from unittest.mock import Mock
import uuid

import veritas_currency_trade_funding as F
import veritas_currency_trade_ledger as L

D = Decimal
START = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)
END = START+timedelta(hours=1)
NOW = END+timedelta(seconds=10)
NEXT = END+timedelta(hours=2)
ACCOUNT, UID, OWNER = "funding-test-account", "funding-test-cny", 5000000001
KEY = "statement-test-key-distinct-from-approval-64-chars-0123456789"
SPEC = L.InstrumentValuation(UID,D("0.01"),D("10"))


def money(value):
    number = D(value)
    units = int(number)
    return {"currency": "rub", "units": str(units), "nano": int((number-units)*D(10**9))}


def operation(oid="funding-one", amount="-5.25", **changes):
    result = {"id":oid, "parentOperationId":"mutable-parent", "brokerAccountId":ACCOUNT,
              "date":(END-timedelta(seconds=1)).isoformat(), "instrumentUid":UID,
              "type":"OPERATION_TYPE_FUNDING", "state":"OPERATION_STATE_EXECUTED",
              "payment":money(amount)}
    result.update(changes)
    return result


def collect(rows=None, **kwargs):
    reader = Mock(return_value={"items": [operation()] if rows is None else rows})
    return F.collect_snapshot(reader, ACCOUNT, UID, window_start=START, window_end=END, **kwargs)


def complete_receipt(template=None, **updates):
    result = {"version":F.VERSION,"authority":F.AUTHORITY,"account_id":ACCOUNT,
              "instrument_uid":UID,"environment":"sandbox","owner_user_id":OWNER,
              "bound_at":START.isoformat(),"spec_revision":SPEC.revision,
              "window_start":START.isoformat(),"window_end":END.isoformat(),
              "snapshot_id":"snapshot-one","snapshot_digest":"a"*64,"fill_digest":"b"*64,
              "funding_cost_rub":"5.25"}
    if template:
        result.update(template)
    result.update(statement_reference="broker-report-reviewed-2026-10-07",
                  statement_sha256="c"*64,calendar_evidence_reference="official-calendar-reviewed-2026-10-07",
                  calendar_evidence_sha256="d"*64,statement_issued_at=NOW.isoformat(),reviewed_at=NOW.isoformat(),
                  settled_through=result["window_end"],next_settlement_due_at=NEXT.isoformat())
    result.update({key:True for key in F.DECLARATIONS})
    result.update(updates)
    return result


class FundingSnapshotTests(unittest.TestCase):
    def test_actual_funding_sign_and_vm_commission_are_not_reposted(self):
        rows = [operation(),operation("credit","1.25"),
                operation("vm","900",type="OPERATION_TYPE_ACCRUING_VARMARGIN"),
                operation("vm2","-20",type=27),operation("commission","-50",type=19)]
        result=collect(rows)
        self.assertEqual(result["funding_cost_rub"],D("4"))
        self.assertEqual(result["funding_count"],2)
        self.assertEqual(len(result["audit"]),5)
        self.assertTrue(result["complete_fetch"])
        self.assertFalse(result["broker_finality"])

    def test_mutable_ids_and_parent_ids_do_not_change_economic_digest(self):
        a=collect([operation("old")])
        b=collect([operation("new",parentOperationId="new-parent",cursor="different")])
        self.assertEqual(a["snapshot_digest"],b["snapshot_digest"])
        self.assertNotEqual(a["audit"],b["audit"])

    def test_broker_commission19_remains_audit_only_without_double_posting(self):
        result=collect([operation(),operation("commission","-9",type="OPERATION_TYPE_BROKER_FEE")])
        self.assertEqual(result["funding_cost_rub"],D("5.25"))
        self.assertEqual(result["audit"][-1]["classification"],"COMMISSION_NOT_POSTED")

    def test_actual_or_pending_margin_and_other_costs_block_instead_of_becoming_funding(self):
        for kind in (14,66,"OPERATION_TYPE_MARGIN_FEE","OPERATION_TYPE_OTHER_FEE"):
            for state in ("OPERATION_STATE_EXECUTED","OPERATION_STATE_PROGRESS"):
                for changes in ({},{"instrumentUid":""},
                                {"instrumentUid":"","childOperations":[{"instrumentUid":UID,"payment":money("-1")}]}):
                    with self.subTest(kind=kind,state=state,changes=changes),self.assertRaisesRegex(
                            F.FundingError,"NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED"):
                        collect([operation("other-cost","-1",type=kind,state=state,**changes)])

    def test_foreign_cancelled_and_zero_other_costs_can_be_excluded(self):
        for kind in (14,66):
            result=collect([operation(),operation("foreign","-99",type=kind,instrumentUid="foreign"),
                            operation("cancelled","-99",type=kind,state="OPERATION_STATE_CANCELED"),
                            operation("zero","0",type=kind)])
            self.assertEqual(result["funding_cost_rub"],D("5.25"))

    def test_zero_parent_does_not_hide_nonzero_cny_other_fee_child(self):
        row=operation("fee","0",type=66,instrumentUid="",childOperations=[
            {"instrumentUid":UID,"payment":money("-2")},
            {"instrumentUid":"foreign","payment":money("2")}])
        with self.assertRaisesRegex(F.FundingError,"NON_FUNDING_ALLOCATION_COST"):
            collect([row])

    def test_same_id_duplicate_across_pages_is_counted_once(self):
        a=operation()
        reader=Mock(side_effect=[{"hasNext":True,"nextCursor":"page2","items":[a]},
                                 {"hasNext":False,"items":[dict(a,cursor="page2")]}])
        result=F.collect_snapshot(reader,ACCOUNT,UID,window_start=START,window_end=END)
        self.assertEqual(result["funding_cost_rub"],D("5.25"))
        self.assertEqual(result["pages"],2)
        self.assertEqual(reader.call_args.kwargs["cursor"],"page2")

    def test_same_id_conflicting_pages_aborts_entire_read(self):
        reader=Mock(side_effect=[{"hasNext":True,"nextCursor":"2","items":[operation()]},
                                 {"items":[operation(amount="-6")]}])
        with self.assertRaisesRegex(F.FundingError,"CHANGED_DURING_READ"):
            F.collect_snapshot(reader,ACCOUNT,UID,window_start=START,window_end=END)

    def test_equal_payments_with_distinct_ids_preserve_multiplicity(self):
        self.assertEqual(collect([operation("one"),operation("two")])["funding_cost_rub"],D("10.50"))

    def test_local_half_open_interval_prevents_adjacent_window_doublecount(self):
        result=collect([operation("before",date=(START-timedelta(microseconds=1)).isoformat()),
                        operation("at-start",date=START.isoformat()),operation("at-end",date=END.isoformat())])
        self.assertEqual(result["funding_count"],1)

    def test_cancelled_and_pending_payments_do_not_become_settled_costs(self):
        result=collect([operation("cancelled",state="OPERATION_STATE_CANCELED"),
                        operation("pending",state="OPERATION_STATE_PROGRESS")])
        self.assertEqual(result["funding_cost_rub"],0)
        self.assertEqual(result["pending_funding_count"],1)

    def test_children_are_attributed_once_without_adding_parent_cash(self):
        row=operation(amount="-9",instrumentUid="",childOperations=[
            {"instrumentUid":UID,"payment":money("-5")},
            {"instrumentUid":"foreign","payment":money("-4")}])
        self.assertEqual(collect([row])["funding_cost_rub"],5)

    def test_parent_target_with_same_uid_child_is_not_double_counted(self):
        row=operation(amount="-5",childOperations=[{"instrumentUid":UID,"payment":money("-5")}])
        self.assertEqual(collect([row])["funding_cost_rub"],5)

    def test_unattributed_or_inconsistent_children_fail_closed(self):
        cases=[operation(instrumentUid=""),operation(childOperations=[{"payment":money("-5")}]),
               operation(childOperations=[{"instrumentUid":UID,"payment":money("-99")}]),
               operation(instrumentUid="foreign",childOperations=[{"instrumentUid":UID,"payment":money("-5.25")}])]
        for row in cases:
            with self.subTest(row=row),self.assertRaises(F.FundingError):
                collect([row])

    def test_foreign_instrument_funding_does_not_enter_allocation(self):
        self.assertEqual(collect([operation(instrumentUid="unrelated")])["funding_cost_rub"],0)

    def test_unknown_state_wrong_account_or_currency_is_not_tolerated(self):
        for change in ({"state":0},{"brokerAccountId":"another"},{"payment":{"currency":"usd","units":"1"}},
                       {"payment":{"currency":"rub","units":"-1","nano":1}}):
            with self.subTest(change=change),self.assertRaises((F.FundingError,L.LedgerError)):
                collect([operation(**change)])

    def test_page_and_cursor_failures_are_bounded(self):
        for pages,kwargs,code in [([{"hasNext":True,"nextCursor":""}],{},"STALLED"),
              ([{"hasNext":True,"nextCursor":"x"},{"hasNext":True,"nextCursor":"x"}],{},"STALLED"),
              ([{"hasNext":True,"nextCursor":"x"}],{"max_pages":1},"PAGE_LIMIT"),
              ([{"hasNext":"false"}],{},"PAGE_INVALID"),
              ([{"items":[operation("a"),operation("b")]}],{"max_items":1},"ITEM_LIMIT")]:
            with self.subTest(code=code),self.assertRaisesRegex(F.FundingError,code):
                F.collect_snapshot(Mock(side_effect=pages),ACCOUNT,UID,window_start=START,window_end=END,**kwargs)

    def test_proto_default_empty_page_is_only_fetch_complete(self):
        result=collect([])
        self.assertTrue(result["complete_fetch"])
        self.assertFalse(result["broker_finality"])
        self.assertEqual(result["funding_cost_rub"],0)

    def test_limit_two_is_rejected_due_to_documented_cursor_defect(self):
        with self.assertRaisesRegex(F.FundingError,"BOUNDED"):
            collect(page_size=2)

    def test_exact_nanos_and_large_money_retain_all_digits(self):
        rows=[operation(str(i),"-9223372036854775807.123456789") for i in range(10)]
        self.assertEqual(collect(rows)["funding_cost_rub"],D("92233720368547758071.234567890"))


class FundingStatementTests(unittest.TestCase):
    def setUp(self):
        self.receipt=complete_receipt()
        self.expected={key:self.receipt[key] for key in ("version","authority","account_id","instrument_uid",
                      "environment","owner_user_id","bound_at","spec_revision","window_start","window_end",
                      "snapshot_id","snapshot_digest","fill_digest","funding_cost_rub")}

    def validate(self, receipt):
        return F.validate_receipt(receipt,expected=self.expected,now=NOW)

    def test_real_signing_helper_verifies_and_never_claims_broker_finality(self):
        signature=F.sign_statement_receipt(KEY,self.receipt)
        F.verify_statement_signature(KEY,self.receipt,signature)
        self.assertEqual(self.validate(self.receipt)["next_settlement_due_at"],NEXT)

    def test_signature_is_domain_separated_and_changes_on_one_cent_correction(self):
        signature=F.sign_statement_receipt(KEY,self.receipt)
        changed=dict(self.receipt,funding_cost_rub="5.26")
        with self.assertRaisesRegex(F.FundingError,"SIGNATURE_INVALID"):
            F.verify_statement_signature(KEY,changed,signature)
        with self.assertRaisesRegex(F.FundingError,"SIGNATURE_INVALID"):
            F.verify_statement_signature("a-different-key-that-is-at-least-32",self.receipt,signature)

    def test_signed_scope_or_arithmetic_mismatch_still_rejected(self):
        for change in ({"account_id":"other"},{"instrument_uid":"other"},{"environment":"production"},
                       {"owner_user_id":True},{"snapshot_digest":"e"*64},{"funding_cost_rub":"5.26"},
                       {"fill_digest":"f"*64},{"bound_at":(START-timedelta(seconds=1)).isoformat()}):
            changed=dict(self.receipt,**change)
            F.verify_statement_signature(KEY,changed,F.sign_statement_receipt(KEY,changed))
            with self.subTest(change=change),self.assertRaises((F.FundingError,L.LedgerError)):
                self.validate(changed)

    def test_empty_history_and_valid_signature_do_not_replace_explicit_review(self):
        for key in F.DECLARATIONS:
            with self.subTest(key=key),self.assertRaisesRegex(F.FundingError,"DECLARATIONS"):
                self.validate(dict(self.receipt,**{key:False}))

    def test_cutoff_and_review_chronology_are_enforced(self):
        for change in ({"settled_through":NOW.isoformat()},
                       {"statement_issued_at":START.isoformat()},
                       {"reviewed_at":(NOW+timedelta(seconds=1)).isoformat()},
                       {"next_settlement_due_at":NOW.isoformat()}):
            with self.subTest(change=change),self.assertRaisesRegex(F.FundingError,"CUTOFF"):
                self.validate(dict(self.receipt,**change))

    def test_report_and_calendar_evidence_must_both_be_present(self):
        for key in ("statement_sha256","calendar_evidence_sha256","calendar_evidence_reference","statement_reference"):
            with self.subTest(key=key),self.assertRaises((F.FundingError,L.LedgerError)):
                self.validate(dict(self.receipt,**{key:""}))

    def test_stored_snapshot_digest_total_and_exact_large_payment_are_validated(self):
        snapshot=collect([operation(str(i),"-9223372036854775807.123456789") for i in range(10)])
        stored={"payload":L._canonical(snapshot),"snapshot_digest":snapshot["snapshot_digest"],
                "funding_cost_rub":snapshot["funding_cost_rub"],"pending_funding_count":0,
                "window_start":START,"window_end":END}
        F._validate_stored_snapshot(stored)
        changed=deepcopy(stored)
        changed["payload"]["economics"]["funding"][0]["payment_rub"]="-1"
        with self.assertRaisesRegex(F.FundingError,"INTEGRITY"):
            F._validate_stored_snapshot(changed)

    def test_disabled_is_inert_and_unconfigured_attestation_fails_before_database(self):
        connect=Mock(side_effect=AssertionError("no database"))
        reader=Mock(side_effect=AssertionError("no broker"))
        reconciler=F.FundingReconciler(connect,None,reader,environment="sandbox",owner_user_id=OWNER,
                                      statement_key=None)
        self.assertEqual(reconciler.ensure_schema()["status"],"DISABLED")
        self.assertEqual(reconciler.observe(ACCOUNT,UID,window_start=START,window_end=END)["status"],"DISABLED")
        self.assertEqual(reconciler.refresh(ACCOUNT,UID)["status"],"DISABLED")
        self.assertEqual(reconciler.attest(ACCOUNT,UID,receipt={},signature="")["status"],"DISABLED")
        reconciler.enabled=True
        with self.assertRaisesRegex(F.FundingError,"NOT_CONFIGURED"):
            reconciler.attest(ACCOUNT,UID,receipt={},signature="")
        connect.assert_not_called()
        reader.assert_not_called()


TEST_DSN=os.getenv("VERITAS_TRADING_TEST_DSN","")


@unittest.skipUnless(TEST_DSN,"explicit ephemeral PostgreSQL DSN not configured")
class FundingPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.conninfo import conninfo_to_dict
        details=conninfo_to_dict(TEST_DSN)
        if (details.get("dbname")!="ci_ephemeral_test_only" or
                details.get("host","") not in ("","localhost","127.0.0.1","::1","postgres")):
            raise RuntimeError("ONLY_EXPLICIT_LOCAL_EPHEMERAL_DATABASE_ALLOWED")
        self.schema="test_currency_funding_"+uuid.uuid4().hex
        with psycopg.connect(TEST_DSN,autocommit=True) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        def connect():
            c=psycopg.connect(TEST_DSN,autocommit=True,row_factory=dict_row)
            c.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            return c
        self.connect=connect
        self.now=START
        self.ledger=L.CurrencyTradeLedger(connect,enabled=True,clock=lambda:self.now)
        self.ledger.ensure_schema()
        self.ledger.account_bind(ACCOUNT,UID,owner_user_id=OWNER,spec=SPEC,broker_signed_lots=0,
                                broker_snapshot_id="flat",observed_at=START,verified=True)
        self.reader=Mock(return_value={"items":[operation()]})
        self.funding=F.FundingReconciler(connect,self.ledger,self.reader,environment="sandbox",owner_user_id=OWNER,
                                       statement_key=KEY,clock=lambda:self.now,enabled=True)
        self.funding.ensure_schema()
        self.now=NOW

    def drop_schema(self):
        import psycopg
        from psycopg import sql
        if not self.schema.startswith("test_currency_funding_"):
            raise AssertionError("owned test schema required")
        with psycopg.connect(TEST_DSN,autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def fill(self,tid="trade-one",when=None,fee="2"):
        return self.ledger.record_fill(ACCOUNT,UID,trade_id=tid,client_order_id="client-"+tid,
            broker_order_id="order-"+tid,side="BUY",lots_count=1,price=D("12"),
            executed_at=when or START+timedelta(minutes=1),spec=SPEC,fee_rub=D(fee),
            metadata={"action":"OPEN","direction":"LONG","source_identity":{"key":"test"},
                      "stop_price":"11.9","target_price":"12.5","horizon":"5m","canonical_event_id":"test-event"})

    def observe(self,start=START,end=END):
        return self.funding.observe(ACCOUNT,UID,window_start=start,window_end=end)

    def accept(self,observation=None,**updates):
        receipt=complete_receipt((observation or self.observe())["receipt_template"],
                                 statement_issued_at=self.now.isoformat(),reviewed_at=self.now.isoformat(),**updates)
        return self.funding.attest(ACCOUNT,UID,receipt=receipt,signature=F.sign_statement_receipt(KEY,receipt))

    def test_positive_statement_after_fill_releases_dynamic_guard_and_exact_nav(self):
        self.fill()
        self.assertFalse(self.funding.status(ACCOUNT,UID)["reconciled"])
        accepted=self.accept()
        self.assertTrue(accepted["reconciliation"]["reconciled"])
        self.assertFalse(accepted["reconciliation"]["broker_finality"])
        state=self.ledger.snapshot(ACCOUNT,UID,mark_price=D("12.10"),mark_observed_at=self.now,spec=SPEC,
                                   funding_reconciled=True)
        self.assertEqual(state["funding_rub"],D("5.25"))
        self.assertEqual(state["currency_nav_rub"],D("10092.75"))
        self.assertEqual(state["high_water_rub"],D("10092.75"))

    def test_concurrent_signed_receipt_replay_posts_one_delta(self):
        self.fill()
        receipt=complete_receipt(self.observe()["receipt_template"])
        sig=F.sign_statement_receipt(KEY,receipt)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs=list(pool.map(lambda _:self.funding.attest(ACCOUNT,UID,receipt=receipt,signature=sig),range(4)))
        self.assertEqual(sum(x["status"]=="OWNER_ATTESTATION_ACCEPTED" for x in outputs),1)
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {L.FUNDING}").fetchone()["n"],1)

    def test_id_renaming_refresh_preserves_attestation_without_second_cash_posting(self):
        self.fill()
        self.accept()
        self.reader.return_value={"items":[operation("renamed",parentOperationId="renamed-parent")]}
        self.assertTrue(self.funding.refresh(ACCOUNT,UID)["reconciled"])
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT funding_rub FROM {L.ACCOUNTS}").fetchone()["funding_rub"],D("5.25"))

    def test_late_correction_blocks_until_review_and_posts_only_delta(self):
        self.fill()
        self.accept()
        self.reader.return_value={"items":[operation("changed-id",amount="-7")]}
        refreshed=self.observe()
        self.assertEqual(refreshed["reconciliation"]["block_reason"],"SETTLEMENT_HISTORY_CORRECTED")
        accepted=self.accept(refreshed)
        self.assertEqual(accepted["delta_cost_rub"],"1.75")
        self.assertTrue(accepted["reconciliation"]["reconciled"])
        self.reader.return_value={"items":[]}
        corrected=self.accept(self.observe())
        self.assertEqual(corrected["delta_cost_rub"],"-7")
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT funding_rub FROM {L.ACCOUNTS}").fetchone()["funding_rub"],0)

    def test_empty_pages_need_attestation_and_due_boundary_is_not_extended(self):
        self.fill()
        self.reader.return_value={}
        observed=self.observe()
        self.assertFalse(observed["reconciliation"]["reconciled"])
        self.assertEqual(observed["reconciliation"]["bound_at"],START.isoformat())
        self.assertEqual(observed["reconciliation"]["observed_window_end"],END.isoformat())
        self.assertTrue(self.accept(observed)["reconciliation"]["reconciled"])
        self.now=NEXT
        self.funding.refresh(ACCOUNT,UID)
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"SETTLEMENT_CHECKPOINT_DUE")

    def test_gap_overlap_predating_and_future_windows_rejected_before_broker(self):
        for start,end in ((START-timedelta(seconds=1),END),(START+timedelta(seconds=1),END),(START,NOW+timedelta(seconds=1))):
            with self.subTest(start=start),self.assertRaises(F.FundingError):
                self.observe(start,end)
        self.reader.assert_not_called()
        self.observe()
        with self.assertRaisesRegex(F.FundingError,"OVERLAP"):
            self.observe(START,END+timedelta(seconds=1))

    def test_late_execution_invalidates_fill_evidence_but_new_period_trade_uses_calendar(self):
        self.fill()
        self.accept()
        self.fill("new-period",when=END+timedelta(seconds=1))
        self.assertTrue(self.funding.status(ACCOUNT,UID)["reconciled"])
        self.fill("late-history",when=START+timedelta(minutes=2))
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"SETTLEMENT_FILL_EVIDENCE_CHANGED")

    def test_stale_snapshot_and_failed_refresh_do_not_preserve_old_admission(self):
        self.fill()
        self.accept()
        self.now+=timedelta(seconds=61)
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"FUNDING_HISTORY_REFRESH_REQUIRED")
        self.assertTrue(self.funding.refresh(ACCOUNT,UID)["reconciled"])
        self.reader.side_effect=RuntimeError("synthetic unavailable")
        with self.assertRaises(RuntimeError):
            self.funding.refresh(ACCOUNT,UID)
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"FUNDING_HISTORY_REFRESH_INCOMPLETE")

    def test_scope_and_unmanaged_funding_adjustments_fail_closed(self):
        self.fill()
        self.accept()
        wrong=F.FundingReconciler(self.connect,self.ledger,self.reader,environment="production",owner_user_id=OWNER,
                                 statement_key=KEY,clock=lambda:self.now,enabled=True)
        self.assertEqual(wrong.status(ACCOUNT,UID)["block_reason"],"FUNDING_ENVIRONMENT_MISMATCH")
        self.ledger.record_funding(ACCOUNT,UID,adjustment_id="outside-reconciliation",cost_rub=D("1"),occurred_at=END)
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"FUNDING_LEDGER_TOTAL_MISMATCH")

    def test_transaction_rollback_cannot_publish_receipt_without_ledger_delta(self):
        self.fill()
        receipt=complete_receipt(self.observe()["receipt_template"])
        signature=F.sign_statement_receipt(KEY,receipt)
        with self.assertRaisesRegex(RuntimeError,"rollback"):
            with self.connect() as c,c.transaction():
                self.funding.attest_on(c,ACCOUNT,UID,receipt=receipt,signature=signature)
                raise RuntimeError("rollback")
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {F.RECEIPTS}").fetchone()["n"],0)
            self.assertEqual(c.execute(f"SELECT funding_rub FROM {L.ACCOUNTS}").fetchone()["funding_rub"],0)

    def test_contiguous_second_period_extends_frontier_only_with_new_review(self):
        self.fill()
        self.accept()
        second_end=END+timedelta(seconds=5)
        self.reader.return_value={"items":[operation("second","-2",date=(END+timedelta(seconds=1)).isoformat())]}
        observation=self.observe(END,second_end)
        self.assertFalse(observation["reconciliation"]["reconciled"])
        accepted=self.accept(observation)
        self.assertTrue(accepted["reconciliation"]["reconciled"])
        self.assertEqual(accepted["reconciliation"]["funding_cost_rub"],"7.25")
        self.assertEqual(accepted["reconciliation"]["settled_through"],second_end.isoformat())

    def test_pending_operations_and_signed_incorrect_total_cannot_be_attested(self):
        self.fill()
        observation=self.observe()
        with self.assertRaisesRegex(F.FundingError,"SCOPE_OR_SNAPSHOT"):
            self.accept(observation,funding_cost_rub="999")
        self.reader.return_value={"items":[operation(state="OPERATION_STATE_PROGRESS")]}
        with self.assertRaisesRegex(F.FundingError,"PENDING_FUNDING"):
            self.accept(self.observe())

    def test_signed_receipt_does_not_cover_tampered_database_cutoff_or_cost(self):
        self.fill()
        self.accept()
        with self.connect() as c:
            c.execute(f"UPDATE {F.RECEIPTS} SET next_settlement_due_at=%s",(NEXT+timedelta(days=10),))
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"STORED_STATEMENT_OR_SNAPSHOT_INVALID")
        with self.connect() as c:
            c.execute(f"UPDATE {F.RECEIPTS} SET next_settlement_due_at=%s",(NEXT,))
            c.execute(f"UPDATE {F.WINDOWS} SET accepted_cost_rub=999")
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"STORED_STATEMENT_OR_SNAPSHOT_INVALID")

    def test_stored_snapshot_tamper_is_rejected_before_admission_and_attestation(self):
        self.fill()
        observed=self.observe()
        with self.connect() as c:
            c.execute(f"UPDATE {F.SNAPSHOTS} SET funding_cost_rub=999")
        with self.assertRaisesRegex(F.FundingError,"INTEGRITY"):
            self.accept(observed)

    def test_missing_issuer_key_allows_read_but_never_postfill_admission(self):
        self.funding._statement_key=None
        self.assertTrue(self.funding.status(ACCOUNT,UID)["reconciled"])
        self.fill()
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],"STATEMENT_ATTESTATION_NOT_CONFIGURED")
        observed=self.observe()
        self.assertEqual(observed["reconciliation"]["block_reason"],"STATEMENT_ATTESTATION_NOT_CONFIGURED")

    def test_separate_margin_cost_invalidates_admission_without_false_funding_post(self):
        self.fill()
        self.accept()
        self.reader.return_value={"items":[operation(),operation("margin-fee","-2",type=14)]}
        with self.assertRaisesRegex(F.FundingError,"NON_FUNDING_ALLOCATION_COST"):
            self.funding.refresh(ACCOUNT,UID)
        self.assertEqual(self.funding.status(ACCOUNT,UID)["block_reason"],
                         "NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED")
        with self.connect() as c:
            state=c.execute(f"SELECT funding_rub,fees_rub FROM {L.ACCOUNTS}").fetchone()
            self.assertEqual(state["funding_rub"],D("5.25"))
            self.assertEqual(state["fees_rub"],D("2"))

    def _race_refreshes(self, *, old_has_fee, newer_has_fee):
        self.fill()
        self.accept()
        started, release, counter_lock = Event(), Event(), Lock()
        calls = 0
        def reader(*args, **kwargs):
            nonlocal calls
            with counter_lock:
                calls += 1
                first = calls == 1
            if first:
                started.set()
                if not release.wait(timeout=10):
                    raise AssertionError("test did not release the first reader")
            rows = [operation()]
            if (old_has_fee if first else newer_has_fee):
                rows.append(operation("separate-margin-fee","-2",type=14))
            return {"items": rows}
        self.reader.side_effect = reader
        # Both readers intentionally use the same clock timestamp: attempt
        # identity, not timestamp granularity or scheduling delay, must decide.
        with ThreadPoolExecutor(max_workers=1) as pool:
            older = pool.submit(self.funding.refresh, ACCOUNT, UID)
            try:
                self.assertTrue(started.wait(timeout=10), "first fetch started")
                if newer_has_fee:
                    with self.assertRaisesRegex(F.FundingError,"NON_FUNDING_ALLOCATION_COST"):
                        self.funding.refresh(ACCOUNT,UID)
                else:
                    self.assertTrue(self.funding.refresh(ACCOUNT,UID)["reconciled"])
            finally:
                release.set()
            expected = "NON_FUNDING_ALLOCATION_COST" if old_has_fee else "OBSERVATION_SUPERSEDED"
            with self.assertRaisesRegex(F.FundingError,expected):
                older.result(timeout=10)

    def test_older_clean_read_cannot_erase_newer_known_cost_failure(self):
        self._race_refreshes(old_has_fee=False,newer_has_fee=True)
        state=self.funding.status(ACCOUNT,UID)
        self.assertFalse(state["reconciled"])
        self.assertEqual(state["block_reason"],"NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED")

    def test_older_failed_read_cannot_poison_newer_successful_refresh(self):
        self._race_refreshes(old_has_fee=True,newer_has_fee=False)
        self.assertTrue(self.funding.status(ACCOUNT,UID)["reconciled"])


if __name__ == "__main__":
    unittest.main()
