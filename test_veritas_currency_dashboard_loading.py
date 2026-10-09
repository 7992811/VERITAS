"""Missing currency reads must never be displayed as a confirmed empty account."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import veritas_currency_dashboard as CurrencyDashboard


class CurrencyDashboardLoadingTests(unittest.TestCase):
    def test_live_display_switch_is_independent_from_order_proposals(self):
        with patch.dict(os.environ, {"TBANK_ACCOUNT_ID":"12345"}, clear=True):
            self.assertTrue(CurrencyDashboard.enabled())
        with patch.dict(os.environ, {"VERITAS_CURRENCY_DASHBOARD_LIVE_ENABLED":"true"}, clear=True):
            self.assertTrue(CurrencyDashboard.enabled())
        with patch.dict(os.environ, {"VERITAS_CURRENCY_DASHBOARD_LIVE_ENABLED":"false",
                                     "TBANK_ACCOUNT_ID":"12345",
                                     "VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED":"true"}, clear=True):
            self.assertFalse(CurrencyDashboard.enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(CurrencyDashboard.enabled())

    @unittest.skipUnless(shutil.which('node'), 'Node required for dashboard regression')
    def test_currency_positions_survive_partial_reads_and_recover(self):
        tree = ast.parse(Path('veritas_v90_ui.py').read_text())
        html = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == '_CANONICAL_HTML'
                            for target in node.targets))
        # Reuse the existing offline browser stub; do not import/start the app.
        harness = Path('tests/dashboard_loading.cjs').read_text().split('(async()=>{', 1)[0]
        scenario = r'''
(async()=>{
  const names=['Impulse','Aggressive','Champion','Challenger','Currency'];
  const cny={asset:'CNYRUBF',direction:'SHORT',active_trade_id:'currency-current',
    avg_entry_price:12.75,last_price:12.73,target_fraction:1,units:780,payload:{}};
  const full=(open=true)=>({status:'OK',portfolios:names.map(name=>({name,
    nav_rub:name==='Currency'?10020:1000000,total_return_pct:name==='Currency'?.2:0,
    positions:name==='Currency'&&open?[cny]:[],gross_leverage:name==='Currency'&&open?1:0,
    net_exposure:name==='Currency'&&open?-1:0,closed_trades:0,wins:0}))});
  const h=harness(),u=h.ui;
  u.st.selectedPortfolio='Currency';
  h.responses.set('/api/v1/paper-portfolios',{data:full()});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);
  assert.match(h.node('positions').innerHTML,/CNYRUBf/);
  assert.match(h.node('positions').innerHTML,/Short/);
  const displayed=h.node('positions').innerHTML;

  const missing=full(false);
  missing.portfolios=missing.portfolios.filter(p=>p.name!=='Currency');
  h.responses.set('/api/v1/paper-portfolios',{data:missing});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);
  assert.equal(h.node('positions').innerHTML,displayed);
  assert.equal(u.st.portfolios.portfolios.find(p=>p.name==='Currency').nav_rub,10020);
  assert.match(h.node('positionSync').textContent,/задержано/);
  assert.match(h.node('portfolios').innerHTML,/Обновление задержано/);
  assert.doesNotMatch(h.node('portfolios').innerHTML,/Сейчас: вне рынка/);
  console.log('PASS: missing Currency preserves its position, balance and explicit stale state');

  const malformed=full(false);
  delete malformed.portfolios.find(p=>p.name==='Currency').positions;
  h.responses.set('/api/v1/paper-portfolios',{data:malformed});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);

  const unavailable=full(false);
  unavailable.portfolios.find(p=>p.name==='Currency').positions_status='UNAVAILABLE';
  h.responses.set('/api/v1/paper-portfolios',{data:unavailable});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);
  h.responses.set('/api/v1/paper-portfolios',{data:{...full(false),status:'PARTIAL',positions_complete:false}});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);
  h.responses.set('/api/v1/paper-portfolios',{error:true});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);
  console.log('PASS: missing arrays, explicit unavailable/partial markers and network errors cannot clear positions');

  const mismatch=full(false);
  mismatch.portfolios.find(p=>p.name==='Currency').gross_leverage=1;
  h.responses.set('/api/v1/paper-portfolios',{data:mismatch});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);

  const staleNav=full();
  const cur=staleNav.portfolios.find(p=>p.name==='Currency');
  cur.gross_leverage=0;cur.net_exposure=0;
  cur.latest={gross_leverage:0,net_exposure:0};
  h.responses.set('/api/v1/paper-portfolios',{data:staleNav});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,1);
  assert.equal(h.node('openCount').textContent,1);
  assert.match(h.node('portfolios').innerHTML,/1 поз. открыто/);
  console.log('PASS: an actual position remains visible even when the NAV summary is behind');

  const closed=full(false);
  closed.portfolios.forEach(p=>{p.positions_status='COMPLETE';p.positions_checked_at='2026-10-07T11:00:00Z'});
  h.responses.set('/api/v1/paper-portfolios',{data:closed});
  await u.loadPortfolios();
  assert.equal(u.st.positionBook.Currency.length,0);
  assert.equal(h.node('openCount').textContent,0);
  assert.match(h.node('positions').innerHTML,/Открытых позиций нет/);
  assert.equal(h.node('positionSync').textContent,'');
  console.log('PASS: a complete confirmed empty read clears a genuinely closed position');

  const cold=harness();
  cold.ui.st.selectedPortfolio='Currency';
  cold.responses.set('/api/v1/paper-portfolios',{data:missing});
  await cold.ui.loadPortfolios();
  const pending=cold.ui.st.portfolios.portfolios.find(p=>p.name==='Currency');
  assert.equal(pending.positions_status,'UNAVAILABLE');
  assert.equal(pending.nav_rub,undefined);
  assert.equal(pending.total_return_pct,undefined);
  assert.equal(pending.gross_leverage,undefined);
  assert.equal(cold.node('openCount').textContent,'—');
  assert.match(cold.node('portfolios').innerHTML,/Данные не получены/);
  assert.doesNotMatch(cold.node('positions').innerHTML,/Открытых позиций нет/);
  assert.doesNotMatch(cold.node('portfolios').innerHTML,/Сейчас: вне рынка/);

  // A legacy response is accepted when all five genuine books are complete.
  cold.responses.set('/api/v1/paper-portfolios',{data:full(false)});
  await cold.ui.loadPortfolios();
  assert.equal(cold.ui.st.portfolioLoadStatus,'COMPLETE');
  assert.equal(cold.ui.st.portfolios.portfolios.find(p=>p.name==='Currency').positions_status,'COMPLETE');
  assert.match(cold.node('positions').innerHTML,/Открытых позиций нет/);
  assert.doesNotMatch(cold.node('portfolios').innerHTML,/Данные не получены/);
  console.log('PASS: cold missing data shows unknown metrics and recovers without retaining an unavailable marker');

  const cached=harness();
  cached.ui.st.positionBook={Currency:[{...cny,portfolio_name:'Currency'}]};
  cached.ui.st.positionBookReady=true;
  const other=full(false);
  other.portfolios=other.portfolios.filter(p=>p.name!=='Currency');
  other.portfolios[0].positions=[{...cny,asset:'GOLD'}];
  cached.responses.set('/api/v1/paper-portfolios',{data:other});
  await cached.ui.loadPortfolios();
  assert.equal(cached.ui.st.positionBook.Currency.length,1);
  assert.equal(cached.ui.st.positionBook.Impulse.length,1);
  console.log('PASS: first partial read can add known positions without erasing an omitted cached currency book');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', harness + scenario], input=html,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
