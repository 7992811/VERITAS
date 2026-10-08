"""Manual trials against synthetic broker/account evidence and local SQL only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
import uuid
import unittest
from unittest.mock import patch

import veritas_currency_manual as M
import veritas_currency_manual_admission as MA
import veritas_currency_manual_telegram as MT
import veritas_currency_trade_plan as P
import veritas_currency_trading as C
import veritas_trade_approvals as A
import veritas_tbank_trading as T
from veritas_trade_telegram import proposal_text, TradeTelegramBridge
import test_veritas_currency_live_admission as LA_FIXTURE
from test_veritas_currency_notifications import Connection
from test_veritas_currency_trading import ACCOUNT, UID, OWNER, BOT
from test_veritas_tbank_trading import FakeTransport, Response, future, money, order
from test_veritas_trade_telegram import FakeTelegram


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.h = LA_FIXTURE.AuthorityTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        h = self.h
        self.authority = MA.ManualAccountAdmission(h.connect, h.broker, ACCOUNT,
            lambda: h.now, evidence=h.evidence)
        h.authority = self.authority
        self.repo = A.TradeApprovals(lambda: Connection(str(Path(h.tmp.name)/'manual.sqlite')),
            b'offline-manual-approval-key-32-bytes-long', clock=lambda: h.now)
        self.repo.ensure_schema()
        self.transport = FakeTransport()
        self.transport.handlers['FutureBy'] = Response({'instrument': {**future(),
            'initialMarginOnBuy': money('1000'), 'initialMarginOnSell': money('1000')}})
        self.adapter = T.TBankTradingAdapter('synthetic-token-only', transport=self.transport,
            config=T.ExecutionConfig(enabled=True, armed=True,
                allowed_account_ids=frozenset({ACCOUNT}), allowed_instrument_uids=frozenset({UID})))
        self.coordinator = C.CurrencyTradingCoordinator(repository=self.repo, adapter=self.adapter,
            account_id=ACCOUNT, owner=C.TradeOwner(OWNER, OWNER, BOT), facts=lambda: h.facts,
            summary=lambda: self.fail('Manual intent must not read the model summary'),
            ingest_execution=lambda *a: None, execution_enabled=True, clock=lambda: h.now,
            live_admission=lambda **kw: self.fail('Manual intent must not claim model admission'),
            manual_admission=self.authority)
        self.request = dict(request_id=str(uuid.uuid4()), action='OPEN', side='SELL', lots=1,
            limit_price=str(h.quote.bid), stop_price='12.400', target_price='12.100', hold_minutes=5)

    def ready(self, change=None):
        # No MODEL document is ever issued. The synthetic account evidence is
        # independently signed with the offline key through the real importer.
        with self.assertRaisesRegex(P.TradePlanBlocked, 'LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED'):
            self.coordinator.prepare_manual(self.request)
        terms = self.authority.status()['evidence_request']['terms']
        self.h.terms = terms
        doc, artifacts = self.h.account_document()
        if change:
            change(doc)
        self.h.publish(doc, artifacts)
        return terms

    def approved(self):
        terms = self.ready()
        row = self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        claim = self.repo.claim_delivery(row['proposal_id'], 'offline-tg')
        delivered = self.repo.mark_delivered(row['proposal_id'], bot_id=BOT, private_chat_id=OWNER,
            message_id=50, terms_hash=row['terms_hash'], delivery_token=claim['delivery_token'])
        return self.repo.decide(delivered['callbacks']['approve'], sender_user_id=OWNER,
            private_chat_id=OWNER, message_id=50, bot_id=BOT, callback_query_id='offline-manual-confirm')

    def test_missing_account_controls_blocks_preparation_without_model_request_or_order(self):
        with self.assertRaisesRegex(P.TradePlanBlocked, 'LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED'):
            self.coordinator.prepare_manual(self.request)
        status = self.authority.status()
        self.assertEqual(status['evidence_request']['required_kinds'], ['ACCOUNT_CONTROLS'])
        self.assertEqual(status['model_admission'], 'NOT_APPLICABLE_OWNER_DECISION')
        self.assertEqual(self.repo.list_pending(account_id=ACCOUNT), [])
        self.assertEqual(self.transport.calls, [])

    def test_buy_geometry_works_and_manual_label_cannot_convert_model_terms(self):
        h = self.h
        request = {**self.request, 'side':'BUY', 'limit_price':str(h.quote.ask),
                   'stop_price':'12.290', 'target_price':'12.600'}
        terms = M.prepare(request, h.facts, h.now)
        self.assertEqual((terms['side'],terms['direction'],terms['lots']), ('BUY','LONG',1))
        with self.assertRaisesRegex(P.TradePlanBlocked, 'MANUAL_PLAN_VERSION_REQUIRED'):
            P.revalidate({**h.terms,'decision_authority':M.VERSION}, h.spec, h.account, h.quote,
                         now=h.now, canonical_event_valid=True)
        h.add_other_future()
        with self.assertRaisesRegex(P.TradePlanBlocked, 'MANUAL_TRIAL_REQUIRES_FLAT_WHOLE_ACCOUNT'):
            self.coordinator.prepare_manual(self.request)
        self.assertEqual(self.transport.calls, [])

    def test_owner_approved_sell_one_is_sent_once_and_duplicate_command_cannot_renew(self):
        terms = self.ready()
        proposal = self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        self.assertEqual(proposal['terms']['side'], 'SELL')
        self.assertEqual(proposal['terms']['lots'], 1)
        self.assertNotIn('entry_context', proposal['terms'])
        self.assertIn('ручная заявка владельца', proposal_text(proposal, execution_enabled=True))
        self.assertEqual(self.coordinator.execute_approved(proposal['proposal_id'])['code'], 'PROPOSAL_NOT_APPROVED')
        self.assertEqual(self.transport.calls, [])
        retry = self.coordinator.prepare_manual(self.request)
        self.assertEqual(retry['terms_hash'], proposal['terms_hash'])
        self.assertEqual(retry['expires_at'], proposal['expires_at'])
        claim = self.repo.claim_delivery(proposal['proposal_id'], 'offline-tg')
        delivered = self.repo.mark_delivered(proposal['proposal_id'], bot_id=BOT, private_chat_id=OWNER,
            message_id=50, terms_hash=proposal['terms_hash'], delivery_token=claim['delivery_token'])
        with self.assertRaises(A.ApprovalError):
            self.repo.decide(delivered['callbacks']['approve'], sender_user_id=OWNER+1,
                private_chat_id=OWNER, message_id=50, bot_id=BOT, callback_query_id='foreign-owner')
        self.repo.decide(delivered['callbacks']['approve'], sender_user_id=OWNER,
            private_chat_id=OWNER, message_id=50, bot_id=BOT, callback_query_id='owner')
        self.transport.handlers['PostOrder'] = lambda body: Response(order(client=body['orderId'], lots=1,
            direction='ORDER_DIRECTION_SELL'))
        self.assertTrue(self.coordinator.execute_approved(proposal['proposal_id'])['ok'])
        self.coordinator.execute_approved(proposal['proposal_id'])
        sends = [x for x in self.transport.calls if x['name']=='PostOrder']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]['body']['quantity'], '1')
        self.assertEqual(sends[0]['body']['direction'], 'ORDER_DIRECTION_SELL')
        self.assertEqual(self.coordinator.prepare_manual(self.request)['proposal_id'], proposal['proposal_id'])

    def test_account_risk_and_switches_are_not_replaced_by_owner_intent(self):
        for change, code in (
            (lambda d: d['payload'].update(kill_switch=True), 'KILL_SWITCH_ACTIVE'),
            (lambda d: d['payload'].update(day_start_unit_nav='1.10', high_water_unit_nav='1.10'), 'DAILY_LOSS_STOP'),
            (lambda d: d['payload'].update(week_start_unit_nav='1.10', high_water_unit_nav='1.10'), 'WEEKLY_LOSS_STOP')):
            with self.subTest(code=code):
                # Independent request/evidence scope for each synthetic case.
                self.h.now += timedelta(seconds=1)
                self.request['request_id'] = str(uuid.uuid4())
                terms = self.ready(change)
                with self.assertRaises(P.TradePlanBlocked):
                    self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
                self.assertIn(code, self.authority.status()['blockers'])
        self.assertEqual(self.transport.calls, [])

    def test_disabled_execution_and_expired_account_evidence_block(self):
        terms = self.ready()
        with patch.dict('os.environ', VERITAS_LIVE_EXECUTION_ARMED='0'):
            with self.assertRaisesRegex(P.TradePlanBlocked, 'LIVE_EXECUTION_NOT_ARMED'):
                self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        self.h.now += timedelta(seconds=31)
        self.h.facts = C.TradeFacts(replace(self.h.spec, observed_at=self.h.now),
            replace(self.h.account, observed_at=self.h.now), replace(self.h.quote, observed_at=self.h.now))
        with self.assertRaises(P.TradePlanBlocked):
            self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        self.assertFalse(self.authority.status()['eligible'])
        self.assertEqual(self.transport.calls, [])

    def test_margin_price_position_expiry_and_quantity_changes_never_send(self):
        terms = M.prepare(self.request, self.h.facts, self.h.now)
        h = self.h
        cases = [
            (replace(h.spec, margin_sell_rub=D('1001')), h.account, h.quote, h.now, 'MARGIN_INCREASE'),
            (h.spec, replace(h.account, available_margin_rub=D('10')), h.quote, h.now, 'INSUFFICIENT_MARGIN'),
            (h.spec, h.account, replace(h.quote, bid=D('12.343')), h.now, 'PRICE_OUTSIDE'),
            (h.spec, replace(h.account, signed_lots=-1, managed_signed_lots=-1), h.quote, h.now, 'POSITION_CHANGED'),
            (h.spec, h.account, replace(h.quote, observed_at=h.now-timedelta(seconds=16)), h.now, 'BROKER_QUOTE_STALE')]
        for spec, account, quote, now, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(P.TradePlanBlocked, code):
                P.revalidate(terms, spec, account, quote, now=now)
        later = h.now+timedelta(seconds=121)
        with self.assertRaisesRegex(P.TradePlanBlocked, 'MANUAL_INTENT_EXPIRED'):
            P.revalidate(terms, replace(h.spec, observed_at=later), replace(h.account, observed_at=later),
                replace(h.quote, observed_at=later), now=later)
        for change in ({'lots':2}, {'limit_price':'12.343'}, {'stop_price':'12.401'},
                       {'policy_version':'stale-policy'}):
            with self.assertRaises(P.TradePlanBlocked):
                P.revalidate({**terms, **change}, h.spec, h.account, h.quote, now=h.now)
        self.assertEqual(self.transport.calls, [])

    def test_manual_close_is_reduce_only_and_does_not_need_entry_evidence(self):
        h = self.h
        entry = M.prepare(self.request, h.facts, h.now)
        account = replace(h.account, signed_lots=-1, managed_signed_lots=-1, ledger_revision=2,
            available_margin_rub=D('0'), costs_reconciled=False)
        facts = C.TradeFacts(h.spec, account, h.quote, entry)
        close = dict(request_id=str(uuid.uuid4()), action='CLOSE', limit_price=str(h.quote.ask))
        terms = M.prepare(close, facts, h.now)
        self.assertEqual((terms['action'], terms['side'], terms['lots']), ('CLOSE','BUY',1))
        self.assertTrue(terms['reduce_only'])
        with self.assertRaises(P.TradePlanBlocked):
            P.revalidate({**terms,'lots':2}, h.spec, account, h.quote, now=h.now)
        with self.assertRaisesRegex(P.TradePlanBlocked,'MANUAL_POSITION_REQUIRED'):
            M.prepare(close, C.TradeFacts(h.spec, account, h.quote, {**entry,'decision_authority':None}), h.now)
        from test_veritas_currency_trade_ledger import fill
        from veritas_currency_trade_ledger import project, InstrumentValuation
        projected = project([fill('manual-sell', 'SELL', 1, '12.344', metadata=entry)], [], [],
                            InstrumentValuation.from_contract(h.spec))
        self.assertEqual(projected['held_terms']['decision_authority'], M.VERSION)
        self.assertEqual(projected['held_terms']['horizon'], 'MANUAL')
        self.assertTrue(projected['metadata_reconciled'])

    def test_telegram_requires_private_owner_and_exact_parameters_without_executing(self):
        calls = []
        tg = FakeTelegram()
        def service(op, body):
            calls.append((op, body))
            if op == 'status':
                return dict(ok=True, enabled=True, account_id=ACCOUNT, instrument_uid=UID,
                            execution_environment='production')
            if op == 'prepare-manual':
                return {'ok':False,'code':'LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED'}
            self.fail('Manual command must not poll or execute: '+op)
        bridge = TradeTelegramBridge(service, tg, OWNER)
        message = dict(message_id=90, text='/currency_manual SELL 1 12,344 12,4 12,1 5',
            **{'from':{'id':OWNER,'is_bot':False}, 'chat':{'id':OWNER,'type':'private'}})
        bridge.handle_message({**message,'chat':{'id':OWNER,'type':'group'}})
        self.assertEqual(calls, [])
        bridge.handle_message(message)
        body = calls[-1][1]
        self.assertEqual(calls[-1][0], 'prepare-manual')
        self.assertEqual(body['request']['side'], 'SELL')
        self.assertEqual(body['request']['limit_price'], '12.344')
        bridge.handle_message(message)
        self.assertEqual(calls[-1][1]['request']['request_id'], body['request']['request_id'])
        count = len([c for c in calls if c[0]=='prepare-manual'])
        bridge.handle_message({**message, 'text':'/currency_manual SELL 2 market'})
        self.assertEqual(len([c for c in calls if c[0]=='prepare-manual']), count)
        self.assertIn('ЛИМИТ СТОП ЦЕЛЬ', tg.sent()[-1]['text'])


if __name__ == '__main__':
    unittest.main()
