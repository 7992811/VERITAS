"""Actual execution-stage notices: durable SQL and fake HTTP/Telegram only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

import veritas_currency_broker_notifications as N
from veritas_currency_broker_delivery import BrokerCurrencyDelivery, start_from_env
from veritas_currency_delivery import DeliveryError
from test_veritas_currency_notifications import Connection

NOW = datetime(2026, 10, 7, 8, tzinfo=timezone.utc)
OWNER, BOT = 123456789, 987654321
KEY = "isolated-broker-notice-service-key-long-enough"
URL = "https://veritas-intelligence-v1.onrender.com"
UID = "c300543d-aa18-4249-b110-615409dde036"


class BrokerNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "notices.sqlite")
        self.now = NOW
        self.connect = lambda: Connection(self.path)
        self.outbox = self.make_outbox()
        self.outbox.ensure_schema()

    def tearDown(self):
        self.temp.cleanup()

    def make_outbox(self, **changes):
        config = dict(connect=self.connect, account_id="synthetic-account", instrument_uid=UID,
                      owner_user_id=OWNER, execution_environment="production", enabled=True, clock=lambda:self.now)
        config.update(changes)
        return N.BrokerNotificationOutbox(**config)

    def proposal(self, *, action="OPEN", direction="LONG", environment="production"):
        side = "BUY" if (direction == "LONG") != (action in ("REDUCE", "CLOSE")) else "SELL"
        return dict(account_id="synthetic-account", instrument_uid=UID, owner_user_id=OWNER,
            client_order_id="synthetic-client-order", terms=dict(account_id="synthetic-account", instrument_uid=UID,
                portfolio="Currency", asset="CNYRUBF", execution_environment=environment,
                action=action, direction=direction, side=side))

    def fill(self, trade="trade-1", *, quantity=2, price="12.345", at=NOW):
        return SimpleNamespace(trade_id=trade, lots=quantity, price=Decimal(price), executed_at=at,
                               currency="RUB", price_type="POINT")

    def enqueue(self, *, outbox=None, proposal=None, fill=None, before=0, after=2, fee=None):
        with self.connect() as c, c.transaction():
            return (outbox or self.outbox).enqueue_on(c, proposal=proposal or self.proposal(), fill=fill or self.fill(),
                before_signed_lots=before, after_signed_lots=after, fee_rub=fee)

    def request(self, action, body=None, *, outbox=None):
        return (outbox or self.outbox).handle(N.PREFIX+action, dict(
            chat_id=N.CHANNEL_ID, telegram_chat_id=N.CHANNEL_ID, **(body or {})))

    def rows(self):
        with self.connect() as c:
            return c.execute(f"SELECT * FROM {N.TABLE} ORDER BY event_id").fetchall()

    def claim(self):
        result, status = self.request("claim", {"worker_id":"test"})
        self.assertEqual(status, 200)
        return result["event"]

    def identity(self, event):
        return {"event_id":event["event_id"], "claim_token":event["claim_token"]}

    def test_actual_unique_stages_describe_position_changes_and_original_approved_action(self):
        self.enqueue()
        self.enqueue(fill=self.fill("trade-2", quantity=1), before=2, after=3)
        self.enqueue(proposal=self.proposal(action="CLOSE"), fill=self.fill("trade-3",quantity=1), before=3, after=2)
        self.enqueue(proposal=self.proposal(action="CLOSE"), fill=self.fill("trade-4"), before=2, after=0, fee=Decimal(".25"))
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["OPEN","ADD","REDUCE","CLOSE"])
        self.assertIn("Брокер · фактическое исполнение", rows[0]["text_snapshot"])
        self.assertIn("Подтверждённая операция: OPEN", rows[1]["text_snapshot"])
        self.assertIn("не распределена", rows[0]["text_snapshot"])
        self.assertIn("0.25 ₽", rows[-1]["text_snapshot"])
        self.assertNotIn("Учебный", rows[0]["text_snapshot"])
        self.assertNotIn("synthetic-account", rows[0]["text_snapshot"])
        self.assertNotIn("reply_markup", rows[0]["text_snapshot"])
        for row in rows:
            self.assertIn("не новое предложение", row["text_snapshot"])

    def test_transaction_is_required_and_rollback_removes_notice_with_accounting_mutation(self):
        with self.connect() as c:
            with self.assertRaisesRegex(ValueError, "ACTIVE_LEDGER_TRANSACTION"):
                self.outbox.enqueue_on(c, proposal=self.proposal(), fill=self.fill(), before_signed_lots=0, after_signed_lots=2)
            c.execute("CREATE TABLE ledger_transaction_marker(value TEXT)")
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with c.transaction():
                    c.execute("INSERT INTO ledger_transaction_marker VALUES(%s)", ("new actual fill",))
                    self.outbox.enqueue_on(c, proposal=self.proposal(), fill=self.fill(), before_signed_lots=0, after_signed_lots=2)
                    raise RuntimeError("rollback")
            self.assertEqual(c.execute("SELECT COUNT(*) AS n FROM ledger_transaction_marker").fetchone()["n"], 0)
        self.assertEqual(self.rows(), [])

    def test_replay_and_restart_deduplicate_trade_id_and_conflicting_economics_roll_back(self):
        self.enqueue()
        self.assertIsNone(self.enqueue(outbox=self.make_outbox()))
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaisesRegex(ValueError, "BROKER_NOTICE_EVENT_CONFLICT"):
            self.enqueue(fill=self.fill(price="12.346"))
        self.assertEqual(len(self.rows()), 1)

    def test_disabled_events_are_never_backfilled_after_enablement(self):
        disabled = self.make_outbox(enabled=False)
        self.enqueue(outbox=disabled)
        self.assertEqual(self.rows()[0]["status"], "SKIPPED_UNCONFIGURED")
        self.enqueue()
        self.assertIsNone(self.claim())
        def forbidden():
            self.fail("disabled handler accessed DB")
        result, code = self.request("claim", outbox=self.make_outbox(connect=forbidden, enabled=False))
        self.assertEqual(code, 200)
        self.assertFalse(result["configured"])

    def test_scope_environment_and_actual_fill_transition_are_strict(self):
        mutations = [lambda p:p.update(owner_user_id=OWNER+1),
                     lambda p:p["terms"].update(account_id="foreign"),
                     lambda p:p["terms"].update(execution_environment="sandbox")]
        for mutate in mutations:
            proposal = self.proposal(); mutate(proposal)
            with self.assertRaisesRegex(ValueError, "SCOPE_MISMATCH"):
                self.enqueue(proposal=proposal)
        fill = self.fill(); fill.price_type="CURRENCY"
        with self.assertRaisesRegex(ValueError, "ACTUAL_RUB_POINT_FILL_REQUIRED"):
            self.enqueue(fill=fill)
        self.assertEqual(self.rows(), [])

    def test_late_or_inconsistent_execution_is_reported_without_rolling_back_actual_accounting(self):
        cases = ((self.proposal(), -1, 1), (self.proposal(action="CLOSE"), 0, -2),
                 (self.proposal(action="ADD"), 0, 2), (self.proposal(), 0, 3))
        for index, (proposal, before, after) in enumerate(cases):
            self.enqueue(proposal=proposal, fill=self.fill("late-"+str(index)), before=before, after=after)
        self.assertEqual(len(self.rows()),len(cases))
        for row in self.rows():
            snapshot=json.loads(row["snapshot"])
            self.assertFalse(snapshot["transition_reconciled"])
            self.assertTrue(snapshot["anomaly_codes"])
            self.assertIn("исполнение требует сверки позиции",row["text_snapshot"])
            self.assertIn("Позиция после исполнения требует сверки",row["text_snapshot"])

    def test_sandbox_is_explicit_and_cannot_be_claimed_by_production_or_foreign_owner(self):
        sandbox=self.make_outbox(execution_environment="sandbox")
        self.enqueue(outbox=sandbox, proposal=self.proposal(environment="sandbox"))
        self.assertIn("ПЕСОЧНИЦА", self.rows()[0]["text_snapshot"])
        self.assertIsNone(self.claim())
        event=self.request("claim", outbox=sandbox)[0]["event"]
        foreign=self.make_outbox(execution_environment="sandbox",owner_user_id=OWNER+1)
        self.assertEqual(self.request("begin",self.identity(event),outbox=foreign)[1],409)

    def test_parallel_claims_preserve_oldest_and_allow_only_one_sender(self):
        self.enqueue()
        self.enqueue(fill=self.fill("trade-2",quantity=1),before=2,after=3)
        with ThreadPoolExecutor(max_workers=6) as workers:
            results=list(workers.map(lambda _:self.request("claim")[0]["event"],range(6)))
        self.assertEqual(len([r for r in results if r]),1)
        self.assertEqual([r["status"] for r in self.rows()],["CLAIMED","PENDING"])

    def test_claim_can_expire_before_send_but_sending_becomes_unknown_without_blocking_exit(self):
        self.enqueue(); first=self.claim()
        self.now+=timedelta(seconds=91)
        again=self.claim()
        self.assertNotEqual(first["claim_token"],again["claim_token"])
        self.assertEqual(self.request("begin",self.identity(first))[1],409)
        self.assertEqual(self.request("begin",self.identity(again))[1],200)
        self.assertEqual(self.request("begin",self.identity(again))[1],409)
        self.enqueue(proposal=self.proposal(action="CLOSE"),fill=self.fill("exit",at=self.now),before=2,after=0)
        self.now+=timedelta(seconds=121)
        exit_event=self.claim()
        self.assertEqual(exit_event["kind"],"CLOSE")
        self.assertEqual(self.rows()[0]["status"],"UNKNOWN")
        self.assertEqual(self.request("complete",dict(self.identity(again),status="RETRY",definite_failure=True))[1],409)
        ack=dict(self.identity(again),status="SENT",message_id=101)
        self.assertEqual(self.request("complete",ack)[1],200)
        self.assertTrue(self.request("complete",ack)[0]["idempotent"])

    def test_fact_age_does_not_discard_open_and_completion_requires_exact_channel_and_begin(self):
        self.enqueue(); self.now+=timedelta(days=1)
        event=self.claim()
        self.assertIn("Уведомление с задержкой",event["text"])
        self.assertEqual(self.request("complete",dict(self.identity(event),status="SENT",message_id=101))[1],409)
        bad=dict(self.identity(event),chat_id="-100123",telegram_chat_id=N.CHANNEL_ID)
        self.assertEqual(self.outbox.handle(N.PREFIX+"begin",bad)[1],409)
        self.assertEqual(self.request("begin",self.identity(event))[1],200)
        self.assertEqual(self.request("complete",dict(self.identity(event),status="RETRY"))[0]["status"],"UNKNOWN")

    def delivery(self, *, lose_send=False, lose_ack=False, wrong_bot=False, wrong_chat=False,
                 wrong_bot_name=False, wrong_channel_id=False, channel_type="channel",
                 can_post=True, title="VERITAS max"):
        calls=[]
        state={"lose_ack":lose_ack}
        def respond(request):
            body=json.loads(request.content)
            if request.url.host=="veritas-intelligence-v1.onrender.com":
                self.assertEqual(request.headers["X-Veritas-Trade-Key"],KEY)
                result, code=self.outbox.handle(request.url.path,body)
                if request.url.path.endswith("/complete") and state["lose_ack"]:
                    state["lose_ack"]=False
                    raise httpx.ReadTimeout("isolated ack lost",request=request)
                return httpx.Response(code,json=result)
            self.assertEqual(request.url.host,"api.telegram.org")
            operation=request.url.path.rsplit("/",1)[-1]; calls.append((operation,body))
            if operation=="getMe":result={"id":BOT+int(wrong_bot),"is_bot":True,"username":"other_bot" if wrong_bot_name else "AxednewsI_bot"}
            elif operation=="getChat":result={"id":-100123 if wrong_channel_id else int(N.CHANNEL_ID),"type":channel_type,"title":title,"username":"other" if wrong_chat else "axednewz"}
            elif operation=="getChatMember":result={"status":"administrator","can_post_messages":can_post}
            elif operation=="sendMessage":
                self.assertEqual(self.rows()[0]["status"],"SENDING")
                self.assertNotIn("reply_markup",body)
                if lose_send:raise httpx.ReadTimeout("isolated Telegram response lost",request=request)
                result={"message_id":700,"chat":{"id":int(N.CHANNEL_ID)}}
            else:self.fail("unexpected Telegram operation: "+operation)
            return httpx.Response(200,json={"ok":True,"result":result})
        http=httpx.Client(transport=httpx.MockTransport(respond),trust_env=False,follow_redirects=False)
        sender=BrokerCurrencyDelivery(URL,KEY,"synthetic-not-live-token",bot_id=BOT,client=http,log=lambda _:None)
        self.addCleanup(http.close)
        return sender,calls

    def test_reused_sender_begins_durably_and_ack_retry_never_sends_twice(self):
        self.enqueue();sender,calls=self.delivery(lose_ack=True)
        with self.assertRaises(DeliveryError):sender.once()
        self.assertEqual(self.rows()[0]["status"],"SENT")
        sender.once()
        self.assertEqual(len([op for op,_ in calls if op=="sendMessage"]),1)
        self.assertNotIn("getUpdates",[op for op,_ in calls])

    def test_telegram_ambiguous_send_is_unknown_after_restart_and_never_retried(self):
        self.enqueue();sender,calls=self.delivery(lose_send=True)
        sender.once()
        self.assertEqual(self.rows()[0]["status"],"UNKNOWN")
        restarted,new_calls=self.delivery()
        restarted.once()
        self.assertEqual(len([op for op,_ in calls+new_calls if op=="sendMessage"]),1)

    def test_actual_bot_and_numeric_channel_username_are_checked_before_claim(self):
        self.enqueue()
        for changes in ({"wrong_bot":True},{"wrong_chat":True},{"wrong_bot_name":True},
                        {"wrong_channel_id":True},{"channel_type":"supergroup"},{"can_post":False}):
            sender,calls=self.delivery(**changes)
            with self.assertRaises(DeliveryError):sender.once()
            self.assertNotIn("sendMessage",[op for op,_ in calls])
            self.assertEqual(self.rows()[0]["status"],"PENDING")

    def test_authorized_channel_title_is_display_metadata_and_axed_news_delivers(self):
        self.enqueue()
        sender,calls=self.delivery(title="Axed News")
        self.assertTrue(sender.once())
        self.assertEqual(self.rows()[0]["status"],"SENT")
        self.assertEqual(len([op for op,_ in calls if op=="sendMessage"]),1)
        self.assertEqual(sender.chat_id,N.CHANNEL_ID)

    def test_disabled_worker_never_constructs_transport_or_thread(self):
        with patch.dict(os.environ,{},clear=True),patch("veritas_currency_broker_delivery.BrokerCurrencyDelivery") as factory:
            self.assertIsNone(start_from_env(None,lambda _:None))
            factory.assert_not_called()


if __name__=="__main__":
    unittest.main()
