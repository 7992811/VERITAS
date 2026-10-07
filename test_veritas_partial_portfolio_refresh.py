"""Known positions in repeated partial reads must advance without erasing the book."""
import ast
from pathlib import Path
import shutil
import subprocess
import unittest


class PartialPortfolioRefreshTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for dashboard regression')
    def test_partial_reads_recover_and_never_regress_or_authorize_clearing(self):
        tree = ast.parse(Path('veritas_v90_ui.py').read_text())
        html = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == '_CANONICAL_HTML'
                            for target in node.targets))
        harness = Path('tests/dashboard_loading.cjs').read_text().split('(async()=>{', 1)[0]
        # Exercise the real cache load/save code, including confirmed empty books.
        harness = harness.replace('function harness(){', 'function harness(cache=null){')
        harness = harness.replace('localStorage:{getItem:()=>null,setItem(){}},',
                                  'localStorage:{getItem:()=>cache,setItem(key,value){cache=value}},')
        harness = harness.replace('return {ui:context.testUI,',
                                  'return {savedCache:()=>cache,ui:context.testUI,')
        scenario = r'''
(async()=>{
  const names=['Impulse','Aggressive','Champion','Challenger','Currency'];
  // Entirely invented fixtures: this date, trade ID and all prices/amounts
  // are test constants, not an account export or an observed trading snapshot.
  const at=minute=>'2001-02-03T09:'+String(minute).padStart(2,'0')+':00.000000+00:00';
  const cny=(minute,changes={})=>({asset:'CNYRUBF',direction:'SHORT',active_trade_id:'synthetic-currency-trade',
    avg_entry_price:10,last_price:9.95,units:240,target_fraction:.4,
    last_mark_at:at(minute),price_source_lock:{key:'TBANK_GRPC:CNYRUBF'},price_source_status:'OK',
    total_trade_pnl_rub:42,total_trade_return_pct:.42,trade_result_status:'COMPLETE',
    realized_gross_pnl_rub:32.4,unrealized_pnl_rub:12,trade_fees_rub:2.4,
    payload:{source_locked_mark:{price:9.95,observed_at:at(minute)}},...changes});
  const report=(minute,open=false,partial=false)=>({status:partial?'PARTIAL':'OK',
    positions_complete:!partial,accounting_complete:!partial,positions_checked_at:at(minute),
    portfolios:names.map(name=>({name,positions_status:'COMPLETE',accounting_status:'COMPLETE',
      positions_checked_at:at(minute),nav_rub:name==='Currency'?10042:1000000,
      positions:name==='Currency'&&open?[cny(minute)]:[],gross_leverage:name==='Currency'&&open?.4:0,
      net_exposure:name==='Currency'&&open?-.4:0,closed_trades:0,wins:0}))});
  const load=async(h,d)=>{h.responses.set('/api/v1/paper-portfolios',d);await h.ui.loadPortfolios()};
  const count=h=>(h.ui.st.positionBook.Currency||[]).length;
  const current=h=>h.ui.st.positionBook.Currency[0];
  const book=h=>h.ui.st.portfolios.portfolios.find(p=>p.name==='Currency');

  const afterError=harness();
  await load(afterError,{error:true});
  await load(afterError,{data:report(5,true,true)});
  assert.equal(count(afterError),1);
  assert.match(afterError.node('positions').innerHTML,/Currency · CNYRUBf/);
  assert.equal(afterError.ui.st.portfolioLoadStatus,'STALE');
  assert.equal(afterError.ui.st.portfolios.positions_complete,false);
  assert.equal(afterError.ui.st.portfolios.accounting_complete,false);
  console.log('PASS: an initial failure followed by PARTIAL recovers known currency positions');

  const empty=harness();
  await load(empty,{data:report(0)});
  await load(empty,{data:report(5,true,true)});
  assert.equal(count(empty),1);
  assert.equal(book(empty).gross_leverage,.4);
  assert.equal(empty.node('openCount').textContent,1);
  const newer=report(6,true,true);
  Object.assign(newer.portfolios[4].positions[0],{last_price:9.94,unrealized_pnl_rub:14.4,
    total_trade_pnl_rub:44.4,total_trade_return_pct:.444,
    payload:{source_locked_mark:{price:9.94,observed_at:at(6)}}});
  newer.portfolios[4].nav_rub=10044.4;
  await load(empty,{data:newer});
  assert.equal(current(empty).last_price,9.94);
  assert.equal(current(empty).total_trade_pnl_rub,44.4);
  assert.equal(current(empty).payload.source_locked_mark.price,9.94);
  assert.equal(current(empty).price_source_lock.key,'TBANK_GRPC:CNYRUBF');
  assert.equal(book(empty).nav_rub,10044.4);
  assert.equal(empty.ui.st.portfolioLoadStatus,'STALE');
  console.log('PASS: later PARTIAL reads advance whole position records and coherent book values');

  const before=JSON.stringify(current(empty));
  const missing=report(7,false,true);missing.portfolios.pop();
  await load(empty,{data:missing});
  await load(empty,{data:report(8,false,true)});
  await load(empty,{error:true});
  assert.equal(JSON.stringify(current(empty)),before);
  assert.equal(book(empty).nav_rub,10044.4);
  assert.equal(count(empty),1);
  console.log('PASS: omitted books, partial empty books and errors preserve positions and totals');

  // An old ledger read cannot win merely because it carries a newer quote.
  const older=report(4,true,true);older.portfolios[4].positions[0].last_mark_at=at(9);
  await load(empty,{data:older});
  const oldMark=report(6,true,true);oldMark.portfolios[4].positions[0].last_mark_at=at(3);
  await load(empty,{data:oldMark});
  const undated=report(10,true,true);delete undated.positions_checked_at;
  undated.portfolios.forEach(p=>delete p.positions_checked_at);
  await load(empty,{data:undated});
  assert.equal(JSON.stringify(current(empty)),before);
  await load(empty,{data:report(4)});
  assert.equal(JSON.stringify(current(empty)),before);
  assert.notEqual(empty.ui.st.portfolioLoadStatus,'COMPLETE');
  console.log('PASS: old/undated partial ledger snapshots and same-ledger older marks cannot regress positions');

  const subset=harness(),complete=report(10);
  complete.portfolios[0].positions=[{...cny(10),asset:'BTC'}, {...cny(10),asset:'NQ'}];
  complete.portfolios[0].nav_rub=1000600;
  await load(subset,{data:complete});
  const reduced=report(11,false,true);
  reduced.portfolios[0].positions=[{...cny(11),asset:'NQ',last_price:9.5,total_trade_pnl_rub:55}];
  reduced.portfolios[0].nav_rub=900000;
  await load(subset,{data:reduced});
  assert.equal(subset.ui.st.positionBook.Impulse.length,2);
  assert.equal(subset.ui.st.positionBook.Impulse.find(p=>p.asset==='NQ').total_trade_pnl_rub,55);
  assert.equal(subset.ui.st.portfolios.portfolios[0].nav_rub,1000600);
  assert.equal(subset.ui.st.portfolios.portfolios[0].accounting_status,'UNAVAILABLE');
  console.log('PASS: partial asset subsets retain omitted positions and cannot replace whole-book accounting');

  const laterBtc=report(30,false,true);
  laterBtc.portfolios[0].positions=[{...cny(30),asset:'BTC',units:600}];
  await load(subset,{data:laterBtc});
  const independentNq=report(20,false,true);
  independentNq.portfolios[0].positions=[{...cny(20),asset:'NQ',units:900}];
  await load(subset,{data:independentNq});
  assert.equal(subset.ui.st.positionBook.Impulse.find(p=>p.asset==='BTC').units,600);
  assert.equal(subset.ui.st.positionBook.Impulse.find(p=>p.asset==='NQ').units,900);
  console.log('PASS: a later partial observation of another asset does not suppress this asset update');

  // A current full ledger closes/changes quantities even if a reported mark is older.
  const ledger=harness(),oldLedger=report(10,true);
  oldLedger.portfolios[4].positions[0].last_mark_at=at(15);
  oldLedger.portfolios[0].positions=[{...cny(15),asset:'ETH'}];
  await load(ledger,{data:oldLedger});
  const newLedger=report(20,true);
  Object.assign(newLedger.portfolios[4].positions[0],{units:720,last_mark_at:at(14),
    total_trade_pnl_rub:31.25,last_price:9.6});
  await load(ledger,{data:newLedger});
  assert.equal(ledger.ui.st.portfolioLoadStatus,'COMPLETE');
  assert.equal(ledger.ui.st.positionBook.Impulse.length,0);
  assert.equal(current(ledger).units,720);
  assert.equal(current(ledger).last_price,9.6);
  assert.equal(current(ledger).total_trade_pnl_rub,31.25);
  assert.equal(current(ledger).price_source_status,'STALE_REPORTED_MARK');
  assert.match(ledger.node('positions').innerHTML,/оценка требует обновления/);
  const partialLedger=report(21,true,true);
  Object.assign(partialLedger.portfolios[4].positions[0],{units:840,last_mark_at:at(13),total_trade_pnl_rub:62.5,price_source_status:'SOURCE_MISMATCH'});
  await load(ledger,{data:partialLedger});
  assert.equal(current(ledger).units,840);
  assert.equal(current(ledger).total_trade_pnl_rub,62.5);
  assert.equal(current(ledger).price_source_status,'SOURCE_MISMATCH');
  console.log('PASS: an older quote cannot veto current ledger quantities or a confirmed close');

  await load(empty,{data:report(12)});
  assert.equal(count(empty),0);
  assert.equal(empty.ui.st.portfolioLoadStatus,'COMPLETE');
  assert.equal(empty.node('openCount').textContent,0);
  await load(empty,{data:report(11,true,true)});
  assert.equal(count(empty),0);
  await load(empty,{data:report(12,true,true)});
  assert.equal(count(empty),0);
  await load(empty,{data:report(11,true)});
  assert.equal(count(empty),0);
  assert.notEqual(empty.ui.st.portfolioLoadStatus,'COMPLETE');
  const restored=harness(empty.savedCache());
  await load(restored,{data:report(11,true,true)});
  assert.equal(count(restored),0);
  assert.doesNotMatch(restored.node('positions').innerHTML,/Currency · CNYRUBf/);
  await load(restored,{data:report(13,true,true)});
  assert.equal(count(restored),1);
  const reopened=harness(restored.savedCache());
  await load(reopened,{data:report(14,true,true)});
  assert.equal(count(reopened),1);
  assert.equal(current(reopened).last_mark_at,at(14));
  assert.equal(reopened.ui.st.portfolios.accounting_complete,false);
  console.log('PASS: confirmed empty reads clear once; cache reload prevents older position resurrection');

  const legacy=harness(JSON.stringify({at:Date.now(),book:{Currency:[cny(25,{updated_at:at(25),opened_at:at(10)})]}}));
  await load(legacy,{data:report(24)});
  assert.equal(count(legacy),1);
  assert.notEqual(legacy.ui.st.portfolioLoadStatus,'COMPLETE');
  await load(legacy,{data:report(24,true,true)});
  assert.equal(current(legacy).last_mark_at,at(25));
  await load(legacy,{data:report(26,true,true)});
  assert.equal(current(legacy).last_mark_at,at(26));
  console.log('PASS: legacy saved position timestamps protect partial updates without inventing a snapshot time');

  const incomplete=report(15,true);incomplete.accounting_complete=false;
  await load(reopened,{data:incomplete});
  assert.notEqual(reopened.ui.st.portfolioLoadStatus,'COMPLETE');
  assert.equal(reopened.ui.st.portfolios.accounting_complete,false);
  await load(reopened,{data:report(16,true)});
  assert.equal(reopened.ui.st.portfolioLoadStatus,'COMPLETE');
  assert.equal(reopened.ui.st.portfolios.accounting_complete,true);
  console.log('PASS: only a complete current read restores a confirmed accounting state');

  // A completed background SQL read can arrive after its freshness window.
  // It still proves the entire book at its original time, not at delivery.
  const delayed=harness();
  await load(delayed,{data:report(30,true)});
  const delayedClose={...report(31),snapshot_stale:true,
    api_source:'completed_snapshot',refresh_status:'UPDATING',cache_age_seconds:11};
  await load(delayed,{data:delayedClose});
  assert.equal(count(delayed),0);
  assert.equal(delayed.ui.st.portfolioLoadStatus,'STALE');
  assert.equal(delayed.ui.st.portfolios.positions_complete,true);
  assert.equal(delayed.ui.st.portfolios.accounting_complete,true);
  assert.equal(delayed.ui.st.positionBookCheckedAt.Currency,Date.parse(at(31)));
  assert.match(delayed.node('positionSync').textContent,/полный состав портфелей на время последнего чтения/);
  const obsoleteOpen={...report(30,true),snapshot_stale:true,
    api_source:'completed_snapshot',refresh_status:'UPDATING',cache_age_seconds:1};
  await load(delayed,{data:obsoleteOpen});
  assert.equal(count(delayed),0);
  assert.equal(delayed.ui.st.positionBookCheckedAt.Currency,Date.parse(at(31)));
  const delayedReload=harness(delayed.savedCache());
  await load(delayedReload,{data:obsoleteOpen});
  assert.equal(count(delayedReload),0);
  await load(delayedReload,{data:report(32,true)});
  const obsoleteClose={...report(31),snapshot_stale:true,api_source:'completed_snapshot'};
  await load(delayedReload,{data:obsoleteClose});
  assert.equal(count(delayedReload),1);
  assert.equal(delayedReload.ui.st.positionBookCheckedAt.Currency,Date.parse(at(32)));
  console.log('PASS: completed delayed books clear at their original time, stay marked stale, and never overwrite newer closes or opens');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', harness + scenario], input=html,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
