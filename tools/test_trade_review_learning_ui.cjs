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
  'globalThis.ui={st,renderReview,reviewSetTab,reviewTradeModel,aggregateMissed,aggregateClosedTradeEvidence,renderAutonomousTradeLearning,hypothesisRuleText};})();'),
  context,{timeout:2000});
const ui=context.ui;
ui.st.trades={self_learning_summary:{reviewed_count:1,evidence_repair_required_count:0},owner_review_queue:[{
  candidate_id:'POST-1',asset:'BTC',direction:'LONG',horizon:'5m',portfolios:['Champion'],
  proposal:{kind:'SUSTAINED_PROFIT_PROTECTION_REPLAY',title:'Проверить защиту прибыли',rationale:'Replay 0,15%',status:'OWNER_REVIEW_REQUIRED',canonical_conflicts:['Не ломать структурный стоп'],promotion_blockers:['OOS_REQUIRED']}
}],self_learning_reviews:[{
  trade_id:'t1',portfolio_name:'Champion',asset:'BTC',horizon:'5m',direction:'LONG',status:'CLOSED',
  opened_at:'2026-10-09T09:00:00Z',closed_at:'2026-10-09T09:30:00Z',
  avg_entry_price:100,avg_exit_price:99.9,net_pnl_rub:-120,gross_pnl_rub:-100,
  mfe_pct:0.22,mae_pct:-0.08,stop_price:99.5,
  self_learning_review:{version:'TRADE_POSTMORTEM_V3',classification:'PROFIT_GIVEBACK_REVIEW',evidence_status:'VERIFIED_RULE_OUTCOME',
    path:{mfe_pct:0.22,mae_pct:-0.08,material_profit_giveback:true},
    levels_volatility:{atr:0.5,initial_risk_atr:1.0,stop_anchor:99.55,stop_anchor_buffer_atr:0.1,initial_target:101.5,target_distance_atr:3.0,gross_target_to_risk:3.0},
    market_context:{trigger_timeframe:'5m',structural_timeframe:'1h',moving_averages:{sma18:100.2,sma50:98.5},moving_averages_in_trade_path:[{name:'SMA18',price:100.2}],indicators:{rsi:58,adx:27}},
    entry_logic:{trade_entry_reason:'TRADE_PLAN_READY'},exit_logic:{exit_reason:'STOP'},issues:['Прибыль отдана'],strengths:['Стоп за low']},
  entry_analysis_snapshot:{}
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
  assets:{BTC:{diagnostics:{
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
assert.equal(elements.reviewBadge.textContent,'3');
assert.match(elements.reviewSummary.innerHTML,/Доказан исход<\/span><b>20<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Доказан путь<\/span><b>8<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Win-rate исходов<\/span><b>60/);
assert.match(elements.reviewSummary.innerHTML,/P&L исходов/);
assert.match(elements.reviewSummary.innerHTML,/Replay Stop \/ Exit<\/span><b>10 \/ 7<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Упущено \/ обучаемо/);
assert.match(elements.reviewSummary.innerHTML,/L2 \/ авто \/ подтвержд\./);
assert.match(elements.reviewBody.innerHTML,/Закрытые сделки · подробный postmortem/);
assert.match(elements.reviewBody.innerHTML,/ATR \/ риск/);
assert.match(elements.reviewBody.innerHTML,/Защищаемый high\/low/);
assert.match(elements.reviewBody.innerHTML,/MA в траектории/);
assert.match(elements.reviewBody.innerHTML,/На утверждение владельца/);
assert.match(elements.reviewBody.innerHTML,/OWNER_REVIEW_REQUIRED/);
assert.match(elements.reviewBody.innerHTML,/outcome-only 12/);
assert.match(elements.reviewBody.innerHTML,/P&L факт \/ cf/);
assert.match(elements.reviewBody.innerHTML,/MFE \/ MAE/);
assert.match(elements.reviewBody.innerHTML,/Вход → выход/);
assert.match(elements.reviewBody.innerHTML,/контрфакт закрыт/);
assert.match(elements.reviewBody.innerHTML,/Автообучение результата и размера/);
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
