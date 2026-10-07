// Direction, plan admission and confirmed portfolio action must remain distinct.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('veritas_v90_ui.py', 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const elements = {};
const context = vm.createContext({document: {
  readyState: 'loading', addEventListener() {}, querySelectorAll: () => [],
  getElementById: id => elements[id] ||= {},
}});
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.ui={planStatus,paperStatus,paperAction,reasonRu,tpDone,tpNotice,renderSignals,renderPortfolios,renderTrades,normalizePosition,st};})();'), context);
const ui = context.ui;
const now = new Date().toISOString();
const signal = {
  asset: 'CNYRUBF', horizon: '5m', research_decision: 'LONG', signal_tier: 'LONG',
  price: 12.751, market_observed_at: now, confidence: .75,
  source_gate_pass: true, market_open: true, paper_eligible: true,
  trade_plan: {eligible: true, stop_price: 12.692, target_price: 12.805,
    final_economics_gate: {status: 'PASS', blockers: []}},
};
const trace = {asset: signal.asset, horizon: signal.horizon, direction: 'LONG',
  signal_observed_at: now, execution: {checked_at: now, status: 'NOT_EXECUTED',
    reason: 'ACCOUNTING_PENDING', execution_action: 'ADD'}};
function render(row = signal, portfolios = []) {
  ui.st.signals = {signals: [row]};
  ui.st.portfolios = {portfolios};
  ui.renderSignals();
  return ui.paperStatus(row);
}
function expectVisibleAction(label) {
  assert.ok(elements.actions.innerHTML.includes('class="admission-label">'+label+'</span>'));
  assert.ok(elements.matrixBody.innerHTML.includes('class="cell-action">'+label+'</span>'));
  assert.match(elements.assets.innerHTML, new RegExp('class="asset-action"[^>]*>'+label+'</small>'));
  assert.match(elements.detail.innerHTML, new RegExp('class="signal-chip"[^>]*>'+label+'</span>'));
}

const blocked = {...signal, trade_plan: {eligible: false,
  entry_timing_gate: {eligible: false, reason: 'SAME_TF_SOURCE_MISMATCH'},
  final_economics_gate: {status: 'BLOCK', blockers: ['NET_REWARD_RISK_BELOW_FLOOR']}}};
assert.equal(render(blocked).ready, false);
expectVisibleAction('Ожидание');
assert.match(elements.actions.innerHTML, /Источник или контракт текущей котировки не совпадает/);
assert.match(elements.actions.innerHTML, /Потенциал после расходов недостаточен/);
assert.doesNotMatch(elements.actions.innerHTML, /ОРДЕР ИСПОЛНЕН|ДОБОР ИСПОЛНЕН/);
assert.match(ui.planStatus({...blocked, source_gate_pass: false}).reason, /Источник или контракт/);
assert.match(ui.planStatus({...signal, final_gate_blockers: ['SAME_TF_SOURCE_MISMATCH'],
  final_gate_status: 'BLOCK'}).reason, /Источник или контракт/);
assert.match(ui.reasonRu('final_economics_gate:SAME_TF_SOURCE_MISMATCH,NET_REWARD_RISK_BELOW_FLOOR'),
  /Источник или контракт.*; Потенциал после расходов/);

// An eligible plan or planned ADD is not an execution receipt.
assert.equal(render({...signal, execution_action: 'ADD', trade_plan: {...signal.trade_plan, execution_action: 'ADD'}}).ready, false);
expectVisibleAction('Ожидание');
assert.equal(render(signal, [{name: 'Currency', admission_trace: [trace]}]).ready, false);
expectVisibleAction('Ожидание');

trace.execution = {checked_at: now, status: 'EXECUTED', reason: 'ORDER_RECORDED', execution_action: 'OPEN'};
assert.equal(render(signal, [{name: 'Currency', admission_trace: [trace]}]).ready, true);
expectVisibleAction('Вход');
assert.match(elements.actions.innerHTML, /Валютный портфель: Ордер исполнен/);
assert.match(elements.actions.innerHTML, /Стоп 12,692 · Цель 12,805/);
trace.execution.execution_action = 'ADD';
assert.equal(render(signal, [{name: 'Currency', admission_trace: [trace]}]).action, 'ADD');
expectVisibleAction('Добор');
assert.match(elements.actions.innerHTML, /ДОБОР ИСПОЛНЕН/);

// A fill in another portfolio must not hide the currency portfolio's refusal.
const refused = {...trace, execution: {checked_at: now, status: 'BLOCKED', reason: 'STOP_RISK_CAP_EXCEEDED'}};
render(signal, [{name: 'Impulse', admission_trace: [trace]}, {name: 'Currency', admission_trace: [refused]}]);
assert.match(elements.actions.innerHTML, /Валютный портфель: Объём ограничен денежным риском всей позиции/);

trace.execution = {checked_at: now, status: 'HELD', reason: 'TARGET_ALREADY_REACHED'};
assert.equal(render(signal, [{name: 'Currency', admission_trace: [trace]}]).ready, false);
expectVisibleAction('Удержание');
assert.match(elements.actions.innerHTML, /Целевой объём позиции уже набран/);
assert.doesNotMatch(elements.actions.innerHTML, /Цена достигла цели/);

// An older receipt or a receipt for a different quote/timeframe cannot certify this entry.
for (const altered of [
  {...trace, execution: {checked_at: new Date(Date.now()-240000).toISOString(), status: 'EXECUTED', execution_action: 'OPEN'}},
  {...trace, execution: {checked_at: now, status: 'EXECUTED', execution_action: 'OPEN'}, horizon: '1h'},
  {...trace, execution: {checked_at: now, status: 'EXECUTED', execution_action: 'OPEN'}, signal_observed_at: '2020-01-01T00:00:00Z'},
]) {
  assert.equal(render(signal, [{name: 'Currency', admission_trace: [altered]}]).ready, false);
  expectVisibleAction('Ожидание');
}
console.log('Direction, admission, entry/add/hold status and contract diagnosis regressions passed');

// Production reason codes must explain why the quote/event cannot authorize an entry.
for (const [code, short, explanation] of [
  ['STRUCTURAL_WAIT_VERIFIED_CROSS', 'пробой', /пересечение ранее известного локального уровня/],
  ['STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET', 'поздно', /слишком большую часть пути до ближайшей цели/],
  ['STRUCTURAL_TARGET_ALREADY_REACHED', 'цель', /Ближайшая цель исходного пробоя уже достигнута/],
  ['STRUCTURAL_HISTORY_INSUFFICIENT', 'свечи', /Недостаточно закрытых свечей/],
  ['STRUCTURAL_ADD_PARENT_MISMATCH', 'ожидание', /защищённую структуру открытой позиции/],
  ['STRUCTURAL_ECONOMICS_QUOTE_MISMATCH', 'цена', /не совпадает с проверенной котировкой/],
  ['STRUCTURAL_TARGET_LADDER_PROVENANCE_MISMATCH', 'ожидание', /доли частичных выходов/],
]) {
  const row = {...signal, trade_entry_eligible: false, trade_entry_reason: code,
    trade_entry_blockers: [code], trade_entry_checked_at: now};
  const status = render(row);
  assert.equal(status.ready, false);
  assert.equal(status.short, short);
  expectVisibleAction('Ожидание');
  assert.match(elements.actions.innerHTML, explanation);
  assert.match(elements.detail.innerHTML, explanation);
}
const runtimeCodes = new Set(['veritas_structural_breakout.py', 'veritas_structural_lifecycle.py']
  .flatMap(path => [...fs.readFileSync(path, 'utf8').matchAll(/["'](STRUCTURAL_[A-Z_]+)["']/g)].map(match => match[1])));
for (const code of runtimeCodes) {
  assert.match(ui.reasonRu(code), /[А-Яа-я]/, code);
  assert.doesNotMatch(ui.reasonRu(code), /Не пройдена дополнительная проверка|Котировка или локальный контекст устарели|Для повторного входа или добора требуется новое рыночное событие/, code);
}
assert.match(ui.reasonRu('final_economics_gate: STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET, STRUCTURAL_TARGET_LADDER_PROVENANCE_MISMATCH'),
  /вход запоздал; Цены или доли частичных выходов/);

// The active historical ladder keeps TP1's recorded price after the runner becomes active.
const ladder = [{kind: 'TP1', price: 12.805, fraction: .5}, {kind: 'TP2', price: 12.845, fraction: .5}];
const partial = {portfolio_name: 'Currency', asset: 'CNYRUBF', direction: 'LONG',
  avg_entry_price: 12.75, last_price: 12.81, stop_price: 12.692, take_price: 12.845,
  target_fraction: .05, payload: {active_target_ladder: ladder, active_target_stage: 1,
    r17_tp1_done: true, r17_tp1_at: '2026-10-07T07:30:00Z', last_target_kind: 'TP1'}};
ui.st.positionBookReady = false;
ui.st.portfolios = {portfolios: [{name: 'Currency', positions: [partial]}]};
ui.renderPortfolios();
assert.match(elements.positions.innerHTML, /TP1 ✓ исполнен<\/span><b>12,805/);
assert.match(elements.positions.innerHTML, /TP2<\/span><b>12,845/);
assert.match(elements.positions.innerHTML, /TP1 · частично исполнен/);

// A recorded final target takes precedence over a stale TP1 partial flag and timestamp.
const final = {...partial, status: 'CLOSED', closed_at: '2026-10-07T08:30:00Z',
  avg_exit_price: 12.845, tp1_done: false, tp1_partial: true,
  tp1_at: '2026-10-07T07:30:00Z', exit_reason: 'TAKE_PROFIT_STRUCTURAL_FINAL',
  payload: {...partial.payload, active_target_stage: 2, last_target_kind: 'FINAL'}};
const normalized = JSON.parse(JSON.stringify(ui.normalizePosition(final, 'Currency', {})));
assert.equal(ui.tpDone(normalized), true);
assert.match(ui.tpNotice(normalized, true), /TP2 · исполнен полностью/);
assert.doesNotMatch(ui.tpNotice(normalized, true), /TP1 · частично|остаток сопровождается/);
ui.st.trades = {trades: [normalized]};
ui.renderTrades();
assert.match(elements.trades.innerHTML, /TP2 · исполнен полностью/);
assert.doesNotMatch(elements.trades.innerHTML, /TP1 · частично/);
const single = {...final, payload: {...final.payload, active_target_ladder: [ladder[0]]}};
assert.match(ui.tpNotice(single), /Тейк · исполнен полностью/);
assert.doesNotMatch(ui.tpNotice(single), /TP2/);
assert.equal(ui.tpNotice({...partial, tp1_done: false, payload: {active_target_ladder: ladder, active_target_stage: 0}}), '');
for (const code of ['STRUCTURAL_PROTECTIVE_EXIT_PENDING', 'PROTECTIVE_EXIT_PENDING']) {
  const pending = {...trace, execution: {checked_at: now, status: 'HELD', reason: code, execution_action: 'HOLD'}};
  render(signal, [{name: 'Currency', admission_trace: [pending]}]);
  expectVisibleAction('Удержание');
  assert.match(elements.actions.innerHTML, /Стоп или цель открытой позиции уже достигнуты; новый вход или добор ожидает исполнения выхода/);
}
console.log('Structural reasons, recorded target ladder and final take-profit UI regressions passed');
