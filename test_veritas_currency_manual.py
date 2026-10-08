"""Manual trials against synthetic broker/account evidence and local SQL only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
import uuid
import unittest
from unittest.mock import patch
import httpx

import veritas_currency_manual as M
import veritas_currency_manual_admission as MA
import veritas_currency_manual_telegram as MT
import veritas_currency_trade_plan as P
import veritas_currency_trading as C
import veritas_trade_approvals as A
import veritas_tbank_trading as T
from veritas_trade_telegram import proposal_text, TradeTelegramBridge
from veritas_trade_telegram import InternalTradeClient, manual_refusal_text
from veritas_currency_trade_service import _error
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

    def ready(self):
        # Explicit owner terms only; no synthetic historical certificate is
        # issued for the current-account manual policy.
        terms = M.prepare(self.request, self.h.facts, self.h.now)
        terms.update(execution_environment='production', manual_owner_user_id=OWNER,
                     manual_private_chat_id=OWNER, manual_bot_id=BOT)
        return A.canonical_terms(terms)

    def approved(self):
        terms = self.ready()
        row = self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        claim = self.repo.claim_delivery(row['proposal_id'], 'offline-tg')
        delivered = self.repo.mark_delivered(row['proposal_id'], bot_id=BOT, private_chat_id=OWNER,
            message_id=50, terms_hash=row['terms_hash'], delivery_token=claim['delivery_token'])
        return self.repo.decide(delivered['callbacks']['approve'], sender_user_id=OWNER,
            private_chat_id=OWNER, message_id=50, bot_id=BOT, callback_query_id='offline-manual-confirm')

    def test_manual_current_account_prepares_without_history_issuer_or_fabricated_metrics(self):
        self.authority.store = LA_FIXTURE.A.LiveAdmissionEvidenceRepository(
            self.h.connect, None, clock=lambda: self.h.now)
        with patch.object(self.authority.store, 'load', side_effect=AssertionError('No historical import')):
            proposal = self.coordinator.prepare_manual(self.request)
        self.assertEqual(proposal['status'], 'PENDING_DELIVERY')
        status = self.authority.status()
        self.assertFalse(status['account_history_required'])
        self.assertTrue(status['eligible'], status['blockers'])
        self.assertNotIn('evidence_request', status)
        self.assertEqual(status['account_risk']['history_status'], 'NOT_CHECKED_OWNER_MANUAL')
        for metric in ('drawdown', 'daily_pnl_pct', 'weekly_pnl_pct'):
            self.assertIsNone(status['account_risk'][metric])
        self.assertEqual(status['model_admission'], 'NOT_APPLICABLE_OWNER_DECISION')
        reads = list(self.h.broker.reads)
        self.authority.status()
        self.assertEqual(self.h.broker.reads, reads)
        self.assertEqual(self.transport.calls, [])

    def test_cached_manual_pass_expires_without_broker_or_history_reads(self):
        self.coordinator.prepare_manual(self.request)
        self.assertTrue(self.authority.status()['eligible'])
        reads = list(self.h.broker.reads)
        self.h.now += timedelta(seconds=16)
        with patch.object(self.authority.store, 'load', side_effect=AssertionError('No historical import')):
            status = self.authority.status()
        self.assertFalse(status['eligible'])
        self.assertEqual(status['status'], 'STALE')
        self.assertIn('LIVE_ADMISSION_RECHECK_REQUIRED', status['blockers'])
        self.assertEqual(self.h.broker.reads, reads)
        self.assertEqual(self.transport.calls, [])

    def test_approved_manual_entry_rechecks_changed_current_account_before_send(self):
        proposal = self.approved()
        reads = len(self.h.broker.reads)
        self.h.broker.equity = D('20000')
        self.h.broker.cash = [money('20000', 'rub')]
        with patch.object(self.authority.store, 'load', side_effect=AssertionError('No historical import')):
            result = self.coordinator.execute_approved(proposal['proposal_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'SINGLE_ASSET_LIMIT')
        self.assertGreater(len(self.h.broker.reads), reads)
        self.assertEqual(self.transport.calls, [])

    def test_screenshot_refusals_keep_exact_numeric_reason_through_http_and_telegram(self):
        h = self.h
        h.facts = replace(h.facts,
            quote=replace(h.quote, bid=D('12.770'), ask=D('12.771')),
            account=replace(h.account, currency_nav_rub=D('10000'), high_water_rub=D('10000')))
        for index, (stop, target, code) in enumerate((
                ('12.90', '12.60', 'MANUAL_ECONOMICS_BLOCKED'),
                ('13.10', '12.30', 'STOP_RISK_CHANGED_AFTER_APPROVAL'))):
            with self.subTest(stop=stop):
                request = {**self.request, 'limit_price':'12.77', 'stop_price':stop,
                           'target_price':target, 'hold_minutes':60}
                with self.assertRaises(P.ManualCheckBlocked) as error:
                    self.coordinator.prepare_manual(request)
                refusal, status = _error(error.exception)
                self.assertEqual((refusal['code'], status), (code, 409))
                self.assertEqual(self.repo.list_pending(account_id=ACCOUNT), [])
                self.assertEqual(self.transport.calls, [])
                def respond(req):
                    if req.url.path.endswith('/status'):
                        return httpx.Response(200, json=dict(ok=True, enabled=True, account_id=ACCOUNT,
                            instrument_uid=UID, execution_environment='production'))
                    self.assertTrue(req.url.path.endswith('/prepare-manual'))
                    return httpx.Response(status, json=refusal)
                tg = FakeTelegram()
                with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                    service = InternalTradeClient('https://veritas-intelligence-v1.onrender.com',
                        'offline-service-key-at-least-32-bytes', client=http)
                    bridge = TradeTelegramBridge(service, tg, OWNER)
                    bridge.handle_message(dict(message_id=91+index,
                        text=f'/currency_manual SELL 1 12.77 {stop} {target} 60',
                        **{'from':{'id':OWNER,'is_bot':False}, 'chat':{'id':OWNER,'type':'private'}}))
                text = tg.sent()[-1]['text']
                self.assertNotIn('конфигурацию доступа', text)
                metrics = refusal['manual_check']
                if index == 0:
                    self.assertLess(D(metrics['net_reward_risk']), D(metrics['minimum_reward_risk']))
                    self.assertIn('минимум: 1,0015', text)
                    self.assertIn('Доход / риск', text)
                else:
                    self.assertEqual(D(metrics['stop_risk_limit_rub']), D('200'))
                    self.assertGreater(D(metrics['stop_risk_rub']), D('350'))
                    self.assertIn('лимит: 200,00 ₽', text)
                    self.assertIn('10000,00 ₽', text)

    def test_refusal_metrics_never_echo_arbitrary_details_or_nonfinite_numbers(self):
        for bad in ('NaN', 'Infinity', 'secret value', '1e999999', '<b>100</b>', {}, True, '9'*99):
            with self.subTest(bad=bad):
                refusal = dict(ok=False, code='STOP_RISK_CHANGED_AFTER_APPROVAL',
                    manual_check=dict(stop_risk_rub=bad, stop_risk_limit_rub='200',
                                      currency_nav_rub='10000', token='secret value'))
                with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(409, json=refusal))) as http:
                    result = InternalTradeClient('https://veritas-intelligence-v1.onrender.com',
                        'offline-service-key-at-least-32-bytes', client=http)('prepare-manual', {})
                self.assertNotIn('stop_risk_rub', result['manual_check'])
                self.assertNotIn('token', result['manual_check'])
                self.assertNotIn('secret', manual_refusal_text(result))
        self.assertNotIn('manual_check', _error(P.TradePlanBlocked('SECRET_ERROR'))[0])

    def test_currency_rr_floor_is_exact_and_shared_by_prepare_and_revalidation(self):
        h = self.h
        facts = replace(h.facts,
            quote=replace(h.quote, bid=D('12.770'), ask=D('12.771')),
            account=replace(h.account, currency_nav_rub=D('10000'), high_water_rub=D('10000')))
        # Adjacent valid price ticks straddle 1.0015 after costs. This checks
        # exact admission rather than a displayed/rounded 1.00 or gross RR.
        request = {**self.request, 'limit_price':'12.77', 'stop_price':'12.90',
                   'target_price':'12.598', 'hold_minutes':60}
        with patch.object(P.VX, 'MIN_REWARD_RISK', 9.0):
            terms = M.prepare(request, facts, h.now)
            P.revalidate(terms, facts.spec, facts.account, facts.quote, now=h.now)
            for horizon in ('MANUAL', '5m', '1h', '1d'):
                plan = P.live_economics_plan('SHORT', D('12.77'), D('12.90'), D('12.598'),
                    facts.quote, horizon, fraction=D('1.277'), expected_hold_seconds=3600)
                gate = P.VX.economics_gate('CNYRUBF', plan, execution_mode='LIVE', now=h.now)
                self.assertEqual(gate['minimum_reward_risk'], 1.0015)
                self.assertTrue(gate['eligible'], gate['blockers'])
                self.assertLess(gate['net_reward_risk'], 1.15)
                self.assertGreater(gate['net_reward_risk'], 1.0015)
                for asset, mode, candidate in (
                        ('GOLD', 'LIVE', plan), ('CNYRUBF', 'PAPER', plan),
                        ('CNYRUBF', 'LIVE', {**plan, 'portfolio':'Champion'})):
                    other = P.VX.economics_gate(asset, candidate, execution_mode=mode, now=h.now)
                    self.assertEqual(other['minimum_reward_risk'], 9.0)
            with self.assertRaises(P.ManualCheckBlocked) as error:
                M.prepare({**request, 'target_price':'12.599'}, facts, h.now)
            metrics = error.exception.manual_check
            self.assertGreater(D(metrics['net_reward_risk']), D('1.001'))
            self.assertLess(D(metrics['net_reward_risk']), D('1.0015'))
            self.assertEqual(metrics['minimum_reward_risk'], '1.0015')
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
        self.assertIn('результат всего счёта не проверяются', proposal_text(proposal, execution_enabled=True))
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

    def test_current_whole_account_limits_and_working_orders_still_block(self):
        self.h.broker.equity = D('20000')
        self.h.broker.cash = [money('20000', 'rub')]
        with self.assertRaisesRegex(P.TradePlanBlocked, 'SINGLE_ASSET_LIMIT'):
            self.coordinator.prepare_manual(self.request)
        self.h.broker.equity = D('1000000')
        self.h.broker.cash = [money('1000000', 'rub')]
        self.h.broker.orders = [{'orderId':'existing-offline-order'}]
        with self.assertRaisesRegex(P.TradePlanBlocked, 'LIVE_WHOLE_ACCOUNT_WORKING_ORDERS_UNRECONCILED'):
            self.coordinator.prepare_manual(self.request)
        self.assertEqual(self.repo.list_pending(account_id=ACCOUNT), [])
        self.assertEqual(self.transport.calls, [])

    def test_disabled_execution_and_stale_current_facts_still_block(self):
        terms = self.ready()
        for name, code in (('VERITAS_LIVE_EXECUTION_ARMED', 'LIVE_EXECUTION_NOT_ARMED'),
                           ('VERITAS_LIVE_EXECUTION_ENABLED', 'LIVE_EXECUTION_DISABLED')):
            with patch.dict('os.environ', {name:'0'}):
                with self.assertRaisesRegex(P.TradePlanBlocked, code):
                    self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        self.h.now += timedelta(seconds=31)
        with self.assertRaises(P.TradePlanBlocked):
            self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        self.assertFalse(self.authority.status()['eligible'])
        self.assertEqual(self.transport.calls, [])

    def test_old_manual_approval_cannot_silently_switch_account_policy(self):
        terms = self.ready()
        terms['plan_version'] = 'currency-owner-manual-v1'
        with self.assertRaisesRegex(P.TradePlanBlocked, 'MANUAL_PLAN_VERSION_REQUIRED'):
            self.coordinator.prepare_manual(self.request, reviewed_terms=terms)
        self.assertEqual(self.repo.list_pending(account_id=ACCOUNT), [])
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
                return {'ok':False,'code':'MANUAL_TRIAL_REQUIRES_FLAT_WHOLE_ACCOUNT'}
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
