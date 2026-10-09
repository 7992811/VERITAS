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
const trade={
  trade_id:'t1',portfolio_name:'Champion',asset:'BTC',horizon:'5m',direction:'LONG',status:'CLOSED',
  opened_at:'2026-10-09T09:00:00Z',closed_at:'2026-10-09T09:30:00Z',
  avg_entry_price:100,avg_exit_price:99.9,net_pnl_rub:-120,gross_pnl_rub:-100,
  payload:{mfe_pct:0.22,mae_pct:-0.08,initial_stop_price:99.5},
  self_learning_review:{
    version:'TRADE_POSTMORTEM_V2',classification:'PROFIT_GIVEBACK_REVIEW',
    material_mfe_threshold_pct:0.15,evidence_status:'UNVERIFIED',
    path:{mfe_pct:0.22,mae_pct:-0.08,material_profit_giveback:true},
    execution:{entry_fill_count:2,exit_fill_count:1,take_profit_fill_count:1,had_adds:true,partial_profit_observed:true},
    levels_volatility:{entry:100,initial_stop:99.5,initial_target:102,atr:1,initial_risk_atr:.5,
      stop_anchor:99.65,protected_level_kind:'previous_low',stop_anchor_buffer_atr:.15,
      target_distance_atr:2,gross_target_to_risk:4,target_ladder:[{price:101},{price:102}]},
    market_context:{trigger_timeframe:'5m',structural_timeframe:'1h',stop_timeframe:'1h',atr_timeframe:'1h',
      moving_averages:{sma18:100.5,sma50:98,sma200:90},
      moving_averages_in_trade_path:[{name:'SMA18',price:100.5}],indicators:{rsi:58,adx:27}},
    issues:['Материальная прибыль была отдана обратно.'],strengths:['Стоп расположен за предыдущим low.'],
    evidence_limitations:['ORDERED_PATH_REPLAY_REQUIRED'],violations:[],
    proposals:[{kind:'EPISODE_ADD_PROFIT_FLOOR',title:'Защитить результат всей идеи при доборах',
      rationale:'Новый ADD не должен превращать защищённый эпизод в отрицательный.',
      status:'OWNER_REVIEW_REQUIRED',canonical_conflicts:['Сохранить трендовое ускорение.'],
      promotion_blockers:['SHADOW_AND_OOS_REQUIRED']}]
  }
};
ui.st.trades={trades:[trade],self_learning_trades:[trade],
  self_learning_owner_review_queue:[{trade_id:'t1',proposal:trade.self_learning_review.proposals[0]}]};
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
assert.match(elements.reviewSummary.innerHTML,/Кандидатов \/ на утверждение<\/span><b>1 \/ 1<\/b>/);
assert.match(elements.reviewSummary.innerHTML,/Опыт \/ shadow<\/span><b>48 \/ 1<\/b>/);
assert.match(elements.reviewBody.innerHTML,/MFE ≥ 0,15%/);
assert.match(elements.reviewBody.innerHTML,/P&L контрфакт/);
assert.match(elements.reviewBody.innerHTML,/не показаны без доказанного ordered-path replay/);
assert.match(elements.reviewBody.innerHTML,/Материальная прибыль была отдана обратно/);
assert.match(elements.reviewBody.innerHTML,/предыдущий low/);
assert.match(elements.reviewBody.innerHTML,/SMA18/);
assert.match(elements.reviewBody.innerHTML,/Защитить результат всей идеи при доборах/);
assert.match(elements.reviewBody.innerHTML,/На утверждение/);
assert.match(elements.reviewBody.innerHTML,/ORDERED_PATH_REPLAY_REQUIRED/);
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
