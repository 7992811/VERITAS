const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('veritas_v90_ui.py', 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const elements = {};
const context = vm.createContext({
  document: {
    readyState: 'loading', addEventListener() {}, querySelectorAll: () => [],
    getElementById: id => elements[id] ||= {},
  },
});
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.ui={planStatus,paperStatus,renderSignals,renderPortfolios,renderTrades,normalizePosition,st};})();'), context);
const ui = context.ui;
const checkedAt = new Date(Date.now()-30000).toISOString();
const signal = {
  asset: 'MOEX', horizon: '1h', research_decision: 'SHORT', signal_tier: 'SUPER_SHORT',
  price: 2229.78, confidence: .80, source_gate_pass: true, market_open: true,
  execution_eligible: false, paper_eligible: true, production_eligible: false,
  trade_entry_valid_until: new Date(Date.now()+90000).toISOString(),
  trade_entry_expiry_reason: 'EXECUTION_QUOTE_STALE',
  trade_plan: {eligible: true, expected_to_stop_ratio: 1.74,
    final_economics_gate: {eligible: true, status: 'PASS', blockers: [], checked_at: checkedAt}},
};
assert.equal(ui.planStatus(signal).ready, true);
assert.equal(ui.planStatus(signal).checked_at, checkedAt);
assert.equal(ui.paperStatus(signal).ready, false);
assert.equal(ui.paperStatus(signal).short, 'проверка');
// The legacy row shape still requires the actual final decision and its clock.
// A source quote or a PASS label alone cannot supply missing admission evidence.
const missingEligibility={...signal,trade_plan:{...signal.trade_plan,
  final_economics_gate:{...signal.trade_plan.final_economics_gate,eligible:undefined}}};
assert.equal(ui.planStatus(missingEligibility).ready,false);
assert.equal(ui.paperStatus(missingEligibility).short,'план');
const missingCheckTime={...signal,market_observed_at:new Date().toISOString(),
  trade_plan:{...signal.trade_plan,final_economics_gate:{
    ...signal.trade_plan.final_economics_gate,checked_at:undefined}}};
assert.equal(ui.planStatus(missingCheckTime).ready,false);
assert.equal(ui.planStatus(missingCheckTime).checked_at,undefined);
assert.match(ui.paperStatus(missingCheckTime).reason,/Нет времени проверки плана/);
for (const [patch, reason] of [
  [{paper_eligible: false}, 'данные'],
  [{trade_plan:{eligible:false,final_economics_gate:{status:'BLOCK',blockers:['QUOTE_TOO_OLD_FOR_HORIZON']}}}, 'цена'],
  [{market_open: false}, 'сессия'],
  [{source_gate_pass: false}, 'данные'],
  [{entry_quality: 'INVALIDATED'}, 'отмена'],
  [{trade_plan: {}}, 'план'],
  [{trade_plan: {eligible: false, final_economics_gate: {
    status: 'BLOCK', blockers: ['RR_BELOW_FINAL_FLOOR']}}}, 'R/R'],
]) {
  const blocked = {...signal, ...patch};
  assert.equal(ui.paperStatus(blocked).ready, false);
  assert.equal(ui.paperStatus(blocked).short, reason);
}
// A good plan must not hide a rejected source gate in any UI surface.
ui.st.signals = {signals: [{...signal, paper_eligible: false}]};
ui.renderSignals();
assert.match(elements.actions.innerHTML, /ВХОД ЗАБЛОКИРОВАН/);
assert.match(elements.matrixBody.innerHTML, /Нет допуска данных для модельной сделки/);
assert.match(elements.detail.innerHTML, /Новый модельный вход пока заблокирован/);
assert.doesNotMatch(elements.detail.innerHTML, /Модельный допуск/);
ui.st.signals = {signals: [signal]};
ui.renderSignals();
assert.match(elements.actions.innerHTML, /ПРОВЕРКА ВХОДА/);
assert.match(elements.detail.innerHTML, /Ожидается решение исполнителя/);
const trace={asset:'MOEX',horizon:'1h',direction:'SHORT',hard_veto:false,
  execution:{checked_at:new Date().toISOString(),status:'BLOCKED',reason:'EXECUTION_QUOTE_UNAVAILABLE',
    quote_gate:{age_seconds:960,max_age_seconds:300}}};
ui.st.portfolios={portfolios:[{name:'Aggressive',admission_trace:[trace]}]};
ui.renderSignals();
assert.equal(ui.paperStatus(signal).ready,false);
assert.match(elements.actions.innerHTML,/возраст 960 с, допустимо 300 с/);
assert.match(elements.detail.innerHTML,/Нет свежей котировки для исполнения/);
assert.doesNotMatch(elements.actions.innerHTML,/МОДЕЛЬНЫЙ ДОПУСК/);
assert.match(elements.actions.innerHTML,/action-reason/);
trace.execution={checked_at:new Date().toISOString(),status:'EXECUTED',reason:'ORDER_RECORDED'};
ui.renderSignals();
assert.equal(ui.paperStatus(signal).ready,true);
assert.match(elements.actions.innerHTML,/ОРДЕР ИСПОЛНЕН/);
trace.execution.checked_at=new Date(Date.now()-240000).toISOString();
assert.equal(ui.paperStatus(signal).ready,false);
trace.execution={checked_at:new Date().toISOString(),status:'HELD',reason:'TARGET_ALREADY_REACHED'};
assert.equal(ui.paperStatus(signal).short,'позиция');
trace.horizon='4h';
assert.equal(ui.paperStatus(signal).short,'другой ТФ');
// Overlapping refusals from multiple portfolios must each appear only once.
const veto={...trace,horizon:'1h',hard_veto:true,
  execution:{checked_at:new Date().toISOString(),status:'NOT_REQUESTED',reason:'NO_NEW_ALLOCATION'}};
ui.st.portfolios={portfolios:[
  {name:'Impulse',admission_trace:[{...veto,profitability_blockers:['LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL','EARLY_BREAKOUT_IN_RANGE_REGIME']}]},
  {name:'Champion',admission_trace:[{...veto,profitability_blockers:['INSUFFICIENT_INDEPENDENT_EVIDENCE','LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL','EARLY_BREAKOUT_IN_RANGE_REGIME']}]},
]};
const vetoReason=ui.paperStatus(signal).reason;
assert.equal(vetoReason.split('; ').length,3);
assert.equal((vetoReason.match(/проверку ожидаемой эффективности/g)||[]).length,1);
assert.match(vetoReason,/выходом из бокового рынка/);
assert.match(ui.paperStatus({...signal,trade_plan:{eligible:false,
  reason:'rule_arbitration_veto:NEGATIVE_VALIDATED_SETUP_EDGE'}}).reason,/Историческая проверка похожих сценариев/);
ui.st.portfolios=null;
console.log('Paper signal UI regressions passed');

const partial = {
  portfolio_name: 'Impulse', asset: 'MOEX', direction: 'LONG', active_trade_id: 'partial',
  avg_entry_price: 100, last_price: 99, target_fraction: .1, take_price: 105,
  total_trade_pnl_rub: 33, total_trade_return_pct: 3.3,
  trade_return_basis: 'ENTRY_NOTIONAL', trade_return_basis_rub: 1000,
  realized_gross_pnl_rub: 50, unrealized_pnl_rub: -10, trade_fees_rub: 5, trade_funding_rub: 2,
  tp1_done: true, tp1_partial: true, tp1_at: '2026-09-29T06:47:17Z', payload: {r17_tp1_done: true},
};
// Normalization and cache round-trip must retain both the fill status and whole-trade result.
ui.st.positionBook = {Impulse: [JSON.parse(JSON.stringify(ui.normalizePosition(partial, 'Impulse', {})))]};
ui.st.portfolios = {portfolios: [{name: 'Impulse', positions: [partial]}]};
ui.renderPortfolios();
assert.match(elements.positions.innerHTML, /ожидаем котировку источника входа/);
const sourcePosition={...partial,price_source_lock:{primary_source:'ProFinance'},price_source_status:'OK',last_mark_at:new Date().toISOString()};
ui.st.positionBook={Impulse:[JSON.parse(JSON.stringify(ui.normalizePosition(sourcePosition,'Impulse',{})))]};
ui.st.portfolios={portfolios:[{name:'Impulse',positions:[sourcePosition]}]};
ui.renderPortfolios();
assert.match(elements.positions.innerHTML, /Источник: ProFinance/);
assert.doesNotMatch(elements.positions.innerHTML, /ожидаем котировку источника входа/);
const noMark=ui.normalizePosition({...sourcePosition,last_price:null,avg_entry_price:100,payload:{source_locked_mark:{price:99}}},'Impulse',{signals:[{asset:'MOEX',price:5000}]});
assert.equal(noMark.last_price,99);
assert.match(elements.positions.innerHTML, /TP1 ✓ исполнен/);
assert.match(elements.positions.innerHTML, /TP1 · частично исполнен/);
assert.doesNotMatch(elements.positions.innerHTML, /Итог сделки/);
assert.doesNotMatch(elements.positions.innerHTML, /NAV/);
assert.match(elements.positions.innerHTML, />33 ₽/);
assert.match(elements.positions.innerHTML, /\+3.30%<small>33 ₽<\/small>/);
assert.ok(elements.positions.innerHTML.indexOf('Вход') < elements.positions.innerHTML.indexOf('Зафиксировано'));
assert.match(elements.positions.innerHTML, /Зафиксировано/);
assert.match(elements.positions.innerHTML, /Переоценка/);
assert.match(elements.positions.innerHTML, /Фондирование/);
ui.st.trades = {trades: [{...partial, status: 'CLOSED', net_pnl_rub: -75,
  total_trade_pnl_rub: -75, total_trade_return_pct: -7.5,
  avg_exit_price: 110, gross_pnl_rub: 120, fees_rub: 192, funding_rub: 3,
  exit_reason: 'TAKE_PROFIT_FULL_MIN_POSITION_R17'}]};
ui.renderTrades();
assert.match(elements.trades.innerHTML, /-7.50%<small>-75 ₽<\/small>/);
assert.doesNotMatch(elements.trades.innerHTML, /NAV|к капиталу/);
assert.doesNotMatch(elements.trades.innerHTML, /10.00%/);
assert.match(elements.trades.innerHTML, /Фиксация по тейку/);
assert.match(elements.trades.innerHTML, /TP1 · частично исполнен/);
console.log('Partial take-profit and whole-trade UI regressions passed');
ui.st.trades = {trades: [{status:'CLOSED',asset:'BTC',net_pnl_rub:10,return_on_entry_nav:.1234}]};
ui.renderTrades();
assert.doesNotMatch(elements.trades.innerHTML,/12.34%/);

for (const [state, net, label] of [
  ['COSTS_NOT_COVERED', -81, 'нет чистой прибыли'],
  ['PROTECTED', 230, 'после расходов'],
  ['UNAVAILABLE', null, 'нет расчёта'],
  ['STOP_REACHED', 50, 'стоп достигнут'],
]) {
  const position = {...partial, profit_protection_active: true,
    net_profit_protection: {version: 'NET_STOP_AFTER_COSTS_V1', state,
      net_at_stop_rub: net, break_even_stop_price: 100.182}};
  ui.st.positionBook = {Impulse: [JSON.parse(JSON.stringify(ui.normalizePosition(position, 'Impulse', {})))]};
  ui.st.portfolios = {portfolios: [{name: 'Impulse', positions: [position]}]};
  ui.renderPortfolios();
  assert.ok(elements.positions.innerHTML.includes('Защита <b>'+label+'</b>'));
  assert.match(elements.positions.innerHTML, /По стопу после расходов/);
  assert.match(elements.positions.innerHTML, /Безубыток/);
  assert.doesNotMatch(elements.positions.innerHTML, /Защита <b>активна/);
}
console.log('Net profit protection UI regressions passed');

// R74: a direction score is not a win probability, and timing/quote failures
// must not disappear behind the generic economics label.
const timingBlocked={...signal,trade_plan:{eligible:false,execution_levels_ready:true,
  entry_timing_gate:{eligible:false,reason:'R66_WAIT_RETEST'},
  final_economics_gate:{status:'BLOCK',blockers:['RR_BELOW_FINAL_FLOOR','TARGET_NOT_PROFITABLE_AFTER_COSTS']}}};
ui.st.portfolios=null;
assert.equal(ui.paperStatus(timingBlocked).short,'ретест');
assert.match(ui.paperStatus(timingBlocked).reason,/Доход до цели не покрывает расходы/);
ui.st.signals={signals:[timingBlocked]};ui.renderSignals();
assert.match(elements.matrixBody.innerHTML,/80.0\/100/);
assert.doesNotMatch(elements.matrixBody.innerHTML,/80.0%/);
assert.match(elements.detail.innerHTML,/Оценка сигнала/);
const goldWait={...timingBlocked,asset:'GOLD',trade_plan:{...timingBlocked.trade_plan,
  stop_price:4836.32,execution_levels_ready:false,
  entry_timing_gate:{eligible:false,reason:'R69_WAIT_LOCAL_BREAKOUT'}}};
assert.equal(ui.paperStatus(goldWait).short,'пробой');
ui.st.selected='GOLD|1h';ui.st.signals={signals:[goldWait]};ui.renderSignals();
assert.doesNotMatch(elements.actions.innerHTML,/4836|4.?836/);
// A current execution rejection must remain visible even if the plan is blocked.
ui.st.portfolios={portfolios:[{name:'Champion',admission_trace:[{
  asset:'MOEX',horizon:'1h',direction:'SHORT',hard_veto:true,reason:'R66_WAIT_RETEST',
  execution:{checked_at:new Date().toISOString(),status:'NOT_REQUESTED',reason:'NO_NEW_ALLOCATION'}
}]}]};
const economicsOnly={...signal,trade_plan:{eligible:false,final_economics_gate:{status:'BLOCK',blockers:['RR_BELOW_FINAL_FLOOR']}}};
assert.equal(ui.paperStatus(economicsOnly).short,'ретест');
assert.match(ui.paperStatus(economicsOnly).reason,/Потенциал относительно риска/);
ui.st.portfolios=null;
assert.equal(ui.paperStatus({...signal,trade_plan:{eligible:true,execution_quote_gate:{eligible:false,reason:'QUOTE_TOO_OLD_FOR_HORIZON'}}}).short,'цена');
console.log('R74 entry diagnosis and signal-score UI regressions passed');


const auditedPosition={...sourcePosition,
  effective_stop_price:98, effective_stop_source:'HARD_STOP',
  tp1_price:105, second_take_price:108, second_take_kind:'TP2',
  next_target_price:105, target_plan_mode:'LADDER',
  execution_timeframe:'1h',
  protection_audit:{
    version:'POSITION_PROTECTION_AUDIT_V1',status:'OK',errors:[],warnings:[],
    distance_to_stop_pct:1.01,distance_to_next_target_pct:6.06,target_reached:false,
    checks:{
      sl:{status:'OK',price:98,distance_pct:1.01,reached:false},
      tp1:{status:'OK',price:105,done:false},
      tp2_or_runner:{status:'OK',kind:'TP2',price:108,mode:'LADDER'},
      source:{status:'OK',quote_status:'OK',locked:true},
      timeframe:{status:'OK',value:'1h'},
      profit_protection:{status:'OK',state:'PROTECTED',active:true,net_at_stop_rub:12},
    },
  },
  net_profit_protection:{version:'NET_STOP_AFTER_COSTS_V1',state:'PROTECTED',
    net_at_stop_rub:12,break_even_stop_price:97.5},
};
ui.st.portfolioLoadStatus='COMPLETE';
ui.st.positionBook={Impulse:[JSON.parse(JSON.stringify(ui.normalizePosition(auditedPosition,'Impulse',{})))]};
ui.st.portfolios={portfolios:[{name:'Impulse',positions:[auditedPosition],positions_status:'COMPLETE'}]};
ui.renderPortfolios();
assert.match(elements.positionAudit.innerHTML,/Защита · <b>OK<\/b>/);
assert.match(elements.positionAudit.innerHTML,/Проверено <b>1\/1<\/b>/);
assert.match(elements.positions.innerHTML,/Защита OK/);
assert.match(elements.positions.innerHTML,/До SL <b>\+1\.01%<\/b>/);
assert.match(elements.positions.innerHTML,/До цели <b>\+6\.06%<\/b>/);
assert.match(elements.positions.innerHTML,/TP2\/Runner <b>TP2<\/b>/);
assert.match(elements.positions.innerHTML,/Источник <b>OK<\/b>/);
assert.match(elements.positions.innerHTML,/TF <b>1h<\/b>/);
assert.match(elements.positions.innerHTML,/Прибыль <b>защищено<\/b>/);

const auditError={...auditedPosition,protection_audit:{...auditedPosition.protection_audit,status:'ERROR',
  errors:['SL_MISSING'],distance_to_stop_pct:null,
  checks:{...auditedPosition.protection_audit.checks,sl:{status:'ERROR',price:null,reached:false}}}};
ui.st.positionBook={Impulse:[JSON.parse(JSON.stringify(ui.normalizePosition(auditError,'Impulse',{})))]};
ui.st.portfolios={portfolios:[{name:'Impulse',positions:[auditError],positions_status:'COMPLETE'}]};
ui.renderPortfolios();
assert.match(elements.positionAudit.innerHTML,/Ошибки <b>1<\/b>/);
assert.match(elements.positions.innerHTML,/Защита ОШИБКА/);
console.log('Open-position protection audit UI regressions passed');
