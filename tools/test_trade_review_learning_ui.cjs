// Regression test for the closed-trade review / missed-opportunity learning tab.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('veritas_v90_ui.py','utf8');
const script=source.split('<script>')[1]?.split('</script>')[0];
assert.ok(script&&/\}\)\(\);\s*$/.test(script),'canonical dashboard script present');
const elements={};
function element(id){
  if(!elements[id])elements[id]={id,innerHTML:'',textContent:'',hidden:false,dataset:{},setAttribute(k,v){this[k]=String(v)}};
  return elements[id];
}
for(const id of ['tradeTab','reviewTab','tradePanel','reviewPanel','reviewBadge','reviewSummary','reviewBody'])element(id);
const context=vm.createContext({
  document:{readyState:'loading',visibilityState:'visible',addEventListener(){},querySelectorAll:()=>[],getElementById:element},
  localStorage:{getItem(){return null},setItem(){}},
  AbortController,
  setTimeout(){return 1},clearTimeout(){},setInterval(){return 1},
  fetch:async()=>({ok:false,status:503,json:async()=>({})}),
});
vm.runInContext(script.replace(/\}\)\(\);\s*$/,
  'globalThis.ui={st,renderReview,reviewSetTab,reviewTradeModel,aggregateMissed,hypothesisRuleText};})();'),
  context,{timeout:2000});
const ui=context.ui;
ui.st.trades={trades:[{
  trade_id:'t1',portfolio_name:'Champion',asset:'BTC',horizon:'5m',direction:'LONG',status:'CLOSED',
  opened_at:'2026-10-09T09:00:00Z',closed_at:'2026-10-09T09:30:00Z',
  avg_entry_price:100,avg_exit_price:99.9,net_pnl_rub:-120,gross_pnl_rub:-100,
  payload:{mfe_pct:0.22,mae_pct:-0.08,initial_stop_price:99.5}
}]};
ui.st.autonomous={counts:{direction:40,trade:8},learning_v2:{
  hypotheses:[{hypothesis_id:'h1',kind:'ENTRY_BLOCKER_RELAXATION',scope:{asset:'BTC',horizon:'5m',regime:'TREND'},
    proposal:{blocker:'RISK_REWARD_GATE',action:'SHADOW_REEVALUATE_AFTER_BLOCK'},
    evidence:{n:9,mean_abs_move:0.006},mode:'SHADOW_ONLY'}],
  assets:{BTC:{diagnostics:{blocked_directional:7,missed_directional_episodes:3,learnable_missed_directional:2,
    hard_veto_missed_directional:1,known_blockers:{RISK_REWARD_GATE:5},top_contexts:[{n:7}],
    zero_entry_candidate_reason:'ENTRY_CANDIDATE_CONDITIONS_PRESENT'}}},
  registry:{candidates:[{candidate_id:'h1',status:'SHADOW_ELIGIBLE',prospective:{n:70,favourable_rate:.61,mean_candidate_signed_return:.004,days:15}}],
    shadow_champions:[{candidate_id:'h1'}]}
}};
ui.renderReview();
assert.equal(elements.reviewBadge.textContent,'4');
assert.match(elements.reviewSummary.innerHTML,/Закрытых в разборе<\/span><b>1<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Упущенных эпизодов<\/span><b class="warn">3<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Опыт \/ shadow<\/span><b>48 \/ 1<\/b>/);
assert.match(elements.reviewBody.innerHTML,/MFE ≥ 0,15%/);
assert.match(elements.reviewBody.innerHTML,/P&L контрфакт/);
assert.match(elements.reviewBody.innerHTML,/не показаны без доказанного ordered-path replay/);
assert.match(elements.reviewBody.innerHTML,/безубыток и дальнейший структурный трейлинг/);
assert.match(elements.reviewBody.innerHTML,/Упущенные возможности/);
assert.match(elements.reviewBody.innerHTML,/Пропущенный вход/);
assert.match(elements.reviewBody.innerHTML,/Подтверждено в shadow/);
assert.match(elements.reviewBody.innerHTML,/Благоприятно/);
assert.match(elements.reviewBody.innerHTML,/61/);
assert.match(elements.reviewBody.innerHTML,/не доказанный исполнимый P&L/);
assert.match(elements.reviewBody.innerHTML,/Production-параметры автоматически не меняются/);
assert.doesNotMatch(elements.reviewBody.innerHTML,/NaN|undefined/);
ui.reviewSetTab('review');
assert.equal(elements.tradePanel.hidden,true);
assert.equal(elements.reviewPanel.hidden,false);
assert.equal(elements.reviewTab['aria-selected'],'true');
console.log('Trade review learning UI: closed trades, missed opportunities, shadow lessons and tabs passed');
