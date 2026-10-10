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
  'globalThis.ui={st,renderReview,reviewSetTab,reviewTradeModel,aggregateMissed,aggregateClosedTradeEvidence,renderAutonomousTradeLearning,learningPriorityQueue,renderLearningPriorityQueue,hypothesisRuleText};})();'),
  context,{timeout:2000});
const ui=context.ui;
ui.st.trades={trades:[{
  trade_id:'t1',portfolio_name:'Champion',asset:'BTC',horizon:'5m',direction:'LONG',status:'CLOSED',
  opened_at:'2026-10-09T09:00:00Z',closed_at:'2026-10-09T09:30:00Z',
  avg_entry_price:100,avg_exit_price:99.9,net_pnl_rub:-120,gross_pnl_rub:-100,
  payload:{mfe_pct:0.22,mae_pct:-0.08,initial_stop_price:99.5}
}]};
ui.st.autonomous={counts:{direction:40,trade:8},
  candidates:[{candidate_id:'a1',kind:'SIZE_DOWN_UNCALIBRATED',state:'evaluating',training_n:32,
    scope:{asset:'BTC',horizon:'5m',regime:'TREND'},evidence_valid:true,profitability_proven:false,
    monitor_evidence:{n:12,days:4,win_rate:.58,mean_delta:.04,base_drawdown_r:2.1,candidate_drawdown_r:1.8},
    reasons:['INSUFFICIENT_TEMPORAL_COVERAGE']}],
  learning_v2:{
  hypotheses:[{hypothesis_id:'h1',kind:'ENTRY_BLOCKER_RELAXATION',scope:{asset:'BTC',horizon:'5m',regime:'TREND'},
    proposal:{blocker:'RISK_REWARD_GATE',action:'SHADOW_REEVALUATE_AFTER_BLOCK'},
    evidence:{n:9,mean_abs_move:0.006},mode:'SHADOW_ONLY'}],
  assets:{BTC:{
    entry_false_block:{blockers:[{blocker:'IMPULSE_ALREADY_PASSED',n:4,observed_move_sum:.032,mean_abs_move:.008}]},
    diagnostics:{
    outcome_evidence_trade_rows:20,path_evidence_trade_rows:8,stop_replay_ready_rows:10,exit_replay_ready_rows:7,
    outcome_profitable_trade_rows:12,outcome_losing_trade_rows:8,outcome_flat_trade_rows:0,
    outcome_win_rate:.60,outcome_net_pnl_rub:540,outcome_avg_net_pnl_rub:27,
    path_avg_mfe_pct:.42,path_avg_mae_pct:-.18,path_avg_capture_ratio:.48,
    blocked_directional:7,missed_directional_episodes:3,learnable_missed_directional:2,
    hard_veto_missed_directional:1,known_blockers:{RISK_REWARD_GATE:5},top_contexts:[{n:7}],
    zero_entry_candidate_reason:'ENTRY_CANDIDATE_CONDITIONS_PRESENT'}}},
  registry:{candidates:[{candidate_id:'h1',status:'SHADOW_ELIGIBLE',prospective:{n:70,favourable_rate:.61,mean_candidate_signed_return:.004,days:15}}],
    shadow_champions:[{candidate_id:'h1'}]}
}};
ui.renderReview();
assert.equal(elements.reviewBadge.textContent,'4');
assert.match(elements.reviewSummary.innerHTML,/Доказан исход<\/span><b>20<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Доказан путь<\/span><b>8<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Win-rate исходов<\/span><b>60/);
assert.match(elements.reviewSummary.innerHTML,/P&L исходов/);
assert.match(elements.reviewSummary.innerHTML,/Replay Stop \/ Exit<\/span><b>10 \/ 7<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Упущено \/ обучаемо/);
assert.match(elements.reviewSummary.innerHTML,/L2 \/ авто \/ подтвержд\./);
assert.match(elements.reviewBody.innerHTML,/Закрытые сделки · плотный разбор/);
assert.match(elements.reviewBody.innerHTML,/outcome-only 12/);
assert.match(elements.reviewBody.innerHTML,/P&L факт \/ cf/);
assert.match(elements.reviewBody.innerHTML,/MFE \/ MAE/);
assert.match(elements.reviewBody.innerHTML,/Вход → выход/);
assert.match(elements.reviewBody.innerHTML,/контрфакт закрыт/);
assert.match(elements.reviewBody.innerHTML,/Автообучение результата и размера/);
assert.match(elements.reviewBody.innerHTML,/Приоритет обучения/);
assert.match(elements.reviewBody.innerHTML,/P2 · повторяется/);
assert.match(elements.reviewBody.innerHTML,/Пропущенный вход/);
assert.match(elements.reviewBody.innerHTML,/3\.20%/);
assert.match(elements.reviewBody.innerHTML,/Размер · без калибровки/);
assert.match(elements.reviewBody.innerHTML,/Future N/);
assert.match(elements.reviewBody.innerHTML,/DD база → кандидат/);
assert.match(elements.reviewBody.innerHTML,/Future validation/);
assert.match(elements.reviewBody.innerHTML,/Replay \/ Shadow/);
assert.match(elements.reviewBody.innerHTML,/Production candidate/);
assert.match(elements.reviewBody.innerHTML,/Упущенные возможности/);
assert.match(elements.reviewBody.innerHTML,/Подтверждено в shadow/);
assert.match(elements.reviewBody.innerHTML,/не доказанный исполнимый P&L/);
assert.doesNotMatch(elements.reviewBody.innerHTML,/NaN|undefined/);
ui.reviewSetTab('review');
assert.equal(elements.tradePanel.hidden,true);
assert.equal(elements.reviewPanel.hidden,false);
assert.equal(elements.reviewTab['aria-selected'],'true');
console.log('Trade review learning UI: closed trades, missed opportunities, shadow lessons and tabs passed');
