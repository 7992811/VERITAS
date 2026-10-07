const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('veritas_v90_ui.py', 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const elements = {};
const originalNow=Date.parse('2026-10-07T20:30:00Z');
let clock=originalNow,nextTimer=0;
const timers=new Map();
class ClockDate extends Date{
  constructor(...args){super(...(args.length?args:[clock]));}
  static now(){return clock;}
}
const context = vm.createContext({Date:ClockDate,
  setTimeout(callback,delay){assert.ok(delay>0&&delay<=2147483647);const id=++nextTimer;timers.set(id,{callback,delay});return id;},
  clearTimeout(id){timers.delete(id);},
  fetch(){throw new Error('expiry repaint must not request the API');},
  document: {
  readyState:'loading',addEventListener(){},querySelectorAll:()=>[],
  getElementById:id=>elements[id]||=( {} ),
}});
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.ui={planStatus,paperStatus,renderSignals,rrOf,st};})();'), context);
const ui=context.ui, now=new ClockDate().toISOString();
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
  trade_entry_valid_until:new ClockDate(originalNow+30000).toISOString(),trade_entry_expiry_reason:'EXECUTION_QUOTE_STALE',
  trade_plan:{eligible:true,expected_to_stop_ratio:1.9,
    final_economics_gate:{eligible:true,status:'PASS',blockers:[],net_reward_risk:1.2}}};
assert.equal(ui.planStatus(ready).ready,true);
assert.equal(ui.paperStatus(ready).ready,false); // A plan is not an execution.
const stale={...ready,snapshot_stale:true};
assert.equal(ui.planStatus(stale).ready,false);
assert.equal(ui.planStatus(stale).short,'обновление');
assert.match(ui.planStatus(stale).reason,/Показан сохранённый сигнал/);
assert.equal(ui.planStatus(stale).checked_at,now);
assert.equal(ui.planStatus({...ready,trade_entry_checked_at:null}).ready,false);
assert.equal(ui.planStatus({...ready,trade_entry_checked_at:'invalid'}).ready,false);
for(const deadline of [undefined,null,'invalid','2026-10-07T20:31:00',Infinity]){
  const row={...ready,trade_entry_valid_until:deadline};
  assert.equal(ui.planStatus(row).ready,false);
  assert.equal(ui.planStatus(row).short,'проверка');
  assert.match(ui.planStatus(row).reason,/Неизвестен срок действия допуска/);
  assert.equal(ui.planStatus(row).checked_at,now);
}
const frozenReady=JSON.stringify(ready);
for(const offset of [29999,30000]){
  clock=originalNow+offset;
  assert.equal(ui.planStatus(ready).ready,true);
}
clock=originalNow+30001;
assert.equal(ui.planStatus(ready).ready,false);
assert.match(ui.paperStatus(ready).reason,/Котировка исполнения устарела/);
assert.equal(ui.planStatus(ready).checked_at,now);
clock=originalNow+600000;
assert.equal(ui.planStatus(ready).ready,false);
assert.equal(JSON.stringify(ready),frozenReady);
clock=originalNow;
for(const code of ['STRUCTURAL_EVENT_EXPIRED','SAME_TF_EVENT_EXPIRED','SAME_TF_CONTEXT_STALE','DELAYED_RESEARCH_QUOTE_STALE']){
  const row={...ready,trade_entry_valid_until:new ClockDate(clock-1).toISOString(),trade_entry_expiry_reason:code};
  assert.equal(ui.planStatus(row).ready,false);
  assert.equal(ui.planStatus(row).checked_at,now);
  assert.match(ui.planStatus(row).reason,code.includes('EVENT')?/Срок входа по этому пробою истёк/:code.includes('CONTEXT')?/Закрытые свечи выбранного периода устарели/:/Котировка исследовательского источника устарела/);
}
assert.equal(ui.planStatus({...base,trade_entry_valid_until:ready.trade_entry_valid_until}).ready,false);
// Old API rows remain blocked until an actual saved final check is available.
const legacy={...ready,trade_entry_eligible:undefined,trade_entry_checked_at:undefined};
assert.equal(ui.planStatus(legacy).ready,false);
legacy.trade_plan={...ready.trade_plan,final_economics_gate:{
  ...ready.trade_plan.final_economics_gate,checked_at:now}};
assert.equal(ui.planStatus(legacy).ready,true);
assert.equal(ui.planStatus({...legacy,trade_entry_valid_until:undefined}).ready,false);
assert.equal(ui.planStatus({...legacy,trade_plan:{...legacy.trade_plan,
  final_economics_gate:{checked_at:now,status:'PASS'}}}).ready,false);
ui.st.signals={status:'warming',at:'2026-10-07T19:28:39+00:00',signals_updated_at:now,signals:[ready]};
ui.renderSignals();
assert.equal(elements.stamp.textContent,'ОБНОВЛЕНО · '+new ClockDate(now).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}));
assert.equal(ui.st.signals.at,'2026-10-07T19:28:39+00:00');
assert.equal(ui.planStatus(ready).ready,true); // A fresh row is not the cold cycle clock.
const trace={asset:'ETH',horizon:'5m',direction:'SHORT',checked_at:now,signal_observed_at:now,
  hard_veto:true,reason:'SAME_TF_CONTEXT_STALE',gross_rr:1.9,net_rr:1.2,
  admission:{open:false,reason:'SAME_TF_CONTEXT_STALE',checked_at:now,blockers:['SAME_TF_CONTEXT_STALE']},
  execution:{status:'NOT_REQUESTED'}};
ui.st.portfolios={portfolios:[{name:'Champion',admission_trace:[trace]}]};
assert.equal(ui.paperStatus(ready).short,'свечи'); // New canonical clock is visible without a fill.
ui.st.signals={signals:[ready]};ui.renderSignals();
assert.match(elements.detail.innerHTML,/R\/R до расходов 1,9 · после расходов 1,2/);
assert.match(elements.detail.innerHTML,/Закрытые свечи выбранного периода устарели/);
trace.checked_at=new ClockDate(clock-240000).toISOString();
assert.equal(ui.paperStatus(ready).short,'проверка');
trace.checked_at=now;trace.signal_observed_at='2020-01-01T00:00:00Z';
assert.equal(ui.paperStatus(ready).short,'проверка'); // Different observation cannot replace this signal.
trace.signal_observed_at=now;trace.hard_veto=false;
trace.admission={open:true,checked_at:now};trace.reason='CANONICAL_SIGNAL_ENTRY';
assert.equal(ui.paperStatus(ready).ready,false);
assert.match(ui.paperStatus(ready).reason,/Исполнение ордера ещё не подтверждено/);
const expiredPlan={...ready,trade_entry_valid_until:new ClockDate(clock-1).toISOString()};
assert.match(ui.paperStatus(expiredPlan).reason,/Котировка исполнения устарела/);
assert.match(ui.paperStatus({...expiredPlan,trade_entry_eligible:false,
  trade_entry_reason:'EXECUTION_QUOTE_STALE',trade_entry_blockers:['EXECUTION_QUOTE_STALE']}).reason,/Котировка исполнения устарела/);
trace.execution={status:'EXECUTED',checked_at:now,reason:'ORDER_RECORDED'};
assert.equal(ui.paperStatus(ready).ready,true);
assert.equal(ui.paperStatus(stale).short,'исполнен'); // A recorded fill remains a fact.
// Recorded execution remains a fact after the display row's source degrades,
// but only for this event/direction/timeframe and a current decision record.
trace.admission.event_id='STF_CURRENT';
for(const deadline of [undefined,'invalid',new ClockDate(clock-1).toISOString()]){
  const row={...ready,trade_entry_valid_until:deadline,
    trade_plan:{...ready.trade_plan,entry_event_snapshot:{event_id:'STF_CURRENT'}}};
  assert.equal(ui.planStatus(row).ready,false);
  assert.equal(ui.paperStatus(row).short,'исполнен');
  assert.equal(ui.paperStatus({...row,trade_plan:{...row.trade_plan,
    entry_event_snapshot:{event_id:'STF_OTHER'}}}).ready,false);
}
const sameEvent={...ready,paper_eligible:false,source_gate_pass:false,
  market_observed_at:'2026-10-07T08:01:00Z',
  trade_plan:{...ready.trade_plan,entry_event_snapshot:{event_id:'STF_CURRENT'}}};
assert.equal(ui.paperStatus(sameEvent).ready,true);
assert.equal(ui.paperStatus({...sameEvent,snapshot_stale:true}).ready,true);
assert.equal(ui.paperStatus({...sameEvent,trade_plan:{...sameEvent.trade_plan,
  entry_event_snapshot:{event_id:'STF_OTHER'}}}).ready,false);
trace.checked_at=new ClockDate(clock-240000).toISOString();
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

// One local timer expires the rendered row even if no new API response arrives.
ui.st.portfolios=null;ui.st.signals={signals:[ready]};ui.renderSignals();
assert.equal(timers.size,1);
const firstTimer=[...timers.keys()][0];
assert.equal(timers.get(firstTimer).delay,30001);
ui.renderSignals();
assert.equal(timers.size,1);
assert.equal(timers.has(firstTimer),false);
const replacement={...ready,trade_entry_valid_until:new ClockDate(clock+60000).toISOString()};
ui.st.signals={signals:[replacement]};ui.renderSignals();
assert.equal(timers.size,1);
assert.equal([...timers.values()][0].delay,60001);
ui.st.signals={signals:[{...replacement,horizon:'4h'},{...ready,horizon:'1h',trade_entry_valid_until:new ClockDate(clock-1).toISOString()},ready]};
ui.renderSignals();
assert.equal(timers.size,1);
assert.equal([...timers.values()][0].delay,30001,'only the nearest future deadline schedules a repaint');
clock=originalNow+30000;ui.renderSignals();
assert.equal([...timers.values()][0].delay,1,'deadline equality remains valid until the next millisecond');
clock=originalNow;
ui.st.signals={signals:[]};ui.renderSignals();assert.equal(timers.size,0);
ui.st.signals={signals:[{...ready,trade_entry_valid_until:'invalid'}]};ui.renderSignals();
assert.equal(timers.size,0);
ui.st.signals={signals:[ready]};ui.renderSignals();
const [timerId,timer]=[...timers.entries()][0];
clock=originalNow+600000;timers.delete(timerId);timer.callback();
assert.equal(timers.size,0,'an expired row must not schedule a zero-delay loop');
assert.equal(ui.planStatus(ready).ready,false);
for(const surface of ['matrixBody','actions','assets','detail'])
  assert.match(elements[surface].innerHTML,/Котировка исполнения устарела/,surface);
assert.equal(JSON.stringify(ready),frozenReady);
assert.equal(ui.planStatus(ready).checked_at,now);
console.log('Research direction, actual admission and net economics UI regressions passed');
