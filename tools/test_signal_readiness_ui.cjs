const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('veritas_v90_ui.py', 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const elements = {};
const context = vm.createContext({document: {
  readyState:'loading',addEventListener(){},querySelectorAll:()=>[],
  getElementById:id=>elements[id]||=( {} ),
}});
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.ui={planStatus,paperStatus,renderSignals,rrOf,st};})();'), context);
const ui=context.ui, now=new Date().toISOString();
const base={asset:'ETH',horizon:'5m',research_decision:'SHORT',signal_tier:'SUPER_SHORT',
  confidence:.7,price:100,source_gate_pass:true,market_open:true,paper_eligible:true,
  execution_eligible:true,market_observed_at:now,trade_entry_eligible:false,
  trade_entry_reason:'SAME_TF_DIRECTION_CONFLICT',trade_entry_checked_at:now,
  trade_entry_blockers:['SAME_TF_DIRECTION_CONFLICT','TARGET_NOT_PROFITABLE_AFTER_COSTS'],
  trade_plan:{eligible:false,execution_levels_ready:false,expected_to_stop_ratio:1.9,
    final_economics_gate:{eligible:false,status:'BLOCK',net_reward_risk:-.12,
      blockers:['SAME_TF_DIRECTION_CONFLICT','TARGET_NOT_PROFITABLE_AFTER_COSTS']}}};
const before=JSON.stringify(base);
ui.st.signals={signals:[base]};ui.renderSignals();
assert.equal(JSON.stringify(base),before);
assert.equal(ui.planStatus(base).ready,false);
assert.equal(ui.paperStatus(base).short,'конфликт');
assert.match(elements.matrixBody.innerHTML,/sig-dot short super/);
assert.match(elements.matrixBody.innerHTML,/Вход: конфликт/);
assert.match(elements.actions.innerHTML,/Прогноз: ↓ Strong Short/);
assert.match(elements.actions.innerHTML,/ВХОД ЗАБЛОКИРОВАН/);
assert.match(elements.actions.innerHTML,/после расходов -0.12/);
assert.doesNotMatch(elements.actions.innerHTML,/после расходов 1.90/);
assert.match(elements.detail.innerHTML,/Прогноз рынка/);
assert.match(elements.detail.innerHTML,/Готовность входа/);
assert.match(elements.detail.innerHTML,/Доход до цели не покрывает расходы/);
assert.match(elements.detail.innerHTML,/Проверка входа:/);

for(const [code,short,message] of [
  ['SAME_TF_CONTEXT_STALE','свечи',/Закрытые свечи выбранного периода устарели/],
  ['SAME_TF_TARGET_ALREADY_REACHED','цель',/Цель исходного события уже достигнута/],
  ['SAME_TF_EVENT_EXPIRED','истёк',/Срок входа по этому пробою истёк/],
  ['SAME_TF_ENTRY_EXTENDED','поздно',/Цена ушла слишком далеко/],
  ['NET_REWARD_RISK_BELOW_FLOOR','R/R',/Потенциал после расходов недостаточен/],
]){
  const row={...base,trade_entry_reason:code,trade_entry_blockers:[code]};
  assert.equal(ui.planStatus(row).short,short);
  assert.match(ui.planStatus(row).reason,message);
}

const ready={...base,trade_entry_eligible:true,trade_entry_reason:'TRADE_PLAN_READY',trade_entry_blockers:[],
  trade_plan:{eligible:true,expected_to_stop_ratio:1.9,
    final_economics_gate:{eligible:true,status:'PASS',blockers:[],net_reward_risk:1.2}}};
assert.equal(ui.planStatus(ready).ready,true);
assert.equal(ui.paperStatus(ready).ready,false); // A plan is not an execution.
const trace={asset:'ETH',horizon:'5m',direction:'SHORT',checked_at:now,signal_observed_at:now,
  hard_veto:true,reason:'SAME_TF_CONTEXT_STALE',gross_rr:1.9,net_rr:1.2,
  admission:{open:false,reason:'SAME_TF_CONTEXT_STALE',checked_at:now,blockers:['SAME_TF_CONTEXT_STALE']},
  execution:{status:'NOT_REQUESTED'}};
ui.st.portfolios={portfolios:[{name:'Champion',admission_trace:[trace]}]};
assert.equal(ui.paperStatus(ready).short,'свечи'); // New canonical clock is visible without a fill.
ui.st.signals={signals:[ready]};ui.renderSignals();
assert.match(elements.detail.innerHTML,/R\/R до расходов 1,9 · после расходов 1,2/);
assert.match(elements.detail.innerHTML,/Закрытые свечи выбранного периода устарели/);
trace.checked_at=new Date(Date.now()-240000).toISOString();
assert.equal(ui.paperStatus(ready).short,'проверка');
trace.checked_at=now;trace.signal_observed_at='2020-01-01T00:00:00Z';
assert.equal(ui.paperStatus(ready).short,'проверка'); // Different observation cannot replace this signal.
trace.signal_observed_at=now;trace.hard_veto=false;
trace.admission={open:true,checked_at:now};trace.reason='CANONICAL_SIGNAL_ENTRY';
assert.equal(ui.paperStatus(ready).ready,false);
assert.match(ui.paperStatus(ready).reason,/Исполнение ордера ещё не подтверждено/);
trace.execution={status:'EXECUTED',checked_at:now,reason:'ORDER_RECORDED'};
assert.equal(ui.paperStatus(ready).ready,true);
// Recorded execution remains a fact after the display row's source degrades,
// but only for this event/direction/timeframe and a current decision record.
trace.admission.event_id='STF_CURRENT';
const sameEvent={...ready,paper_eligible:false,source_gate_pass:false,
  market_observed_at:'2026-10-07T08:01:00Z',
  trade_plan:{...ready.trade_plan,entry_event_snapshot:{event_id:'STF_CURRENT'}}};
assert.equal(ui.paperStatus(sameEvent).ready,true);
assert.equal(ui.paperStatus({...sameEvent,trade_plan:{...sameEvent.trade_plan,
  entry_event_snapshot:{event_id:'STF_OTHER'}}}).ready,false);
trace.checked_at=new Date(Date.now()-240000).toISOString();
assert.equal(ui.paperStatus(sameEvent).ready,false); // Actual top-level clock wins over a fresh nested stub.
trace.checked_at=now;trace.execution={status:'UNKNOWN'};trace.admission={};
trace.reason='ADMISSION_NOT_RECORDED';trace.hard_veto=true;
assert.equal(ui.paperStatus(ready).ready,false);
assert.match(ui.paperStatus(ready).reason,/Решение портфеля по этому сигналу ещё не записано/);

ui.st.portfolios=null;
const missingNet={...ready,trade_plan:{eligible:true,expected_to_stop_ratio:1.9,
  final_economics_gate:{eligible:true,status:'PASS',blockers:[]}}};
assert.equal(ui.rrOf(missingNet),undefined);
ui.st.signals={signals:[missingNet]};ui.renderSignals();
assert.match(elements.actions.innerHTML,/после расходов —/);
assert.doesNotMatch(elements.actions.innerHTML,/после расходов 1.90/);
assert.equal(ui.planStatus({...ready,paper_eligible:false}).ready,false);
console.log('Research direction, actual admission and net economics UI regressions passed');
