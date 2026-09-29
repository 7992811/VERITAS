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
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.ui={paperStatus,renderSignals,st};})();'), context);
const ui = context.ui;
const signal = {
  asset: 'MOEX', horizon: '1h', research_decision: 'SHORT', signal_tier: 'SUPER_SHORT',
  price: 2229.78, confidence: .80, source_gate_pass: true, market_open: true,
  execution_eligible: false, paper_eligible: true, production_eligible: false,
  trade_plan: {eligible: true, expected_to_stop_ratio: 1.74,
    final_economics_gate: {status: 'PASS', blockers: []}},
};
assert.equal(ui.paperStatus(signal).ready, true);
for (const [patch, reason] of [
  [{paper_eligible: false}, 'данные'],
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
assert.match(elements.actions.innerHTML, /МОДЕЛЬНЫЙ ДОПУСК/);
assert.match(elements.detail.innerHTML, /Открытие и размер определяет портфель/);
console.log('Paper signal UI regressions passed');
