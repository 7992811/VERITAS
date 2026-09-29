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
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.ui={paperStatus,renderSignals,renderPortfolios,renderTrades,normalizePosition,st};})();'), context);
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

const partial = {
  portfolio_name: 'Impulse', asset: 'MOEX', direction: 'LONG', active_trade_id: 'partial',
  avg_entry_price: 100, last_price: 99, target_fraction: .1, take_price: 105,
  total_trade_pnl_rub: 33, total_trade_return_pct: .0033,
  realized_gross_pnl_rub: 50, unrealized_pnl_rub: -10, trade_fees_rub: 5, trade_funding_rub: 2,
  tp1_done: true, tp1_partial: true, tp1_at: '2026-09-29T06:47:17Z', payload: {r17_tp1_done: true},
};
// Normalization and cache round-trip must retain both the fill status and whole-trade result.
ui.st.positionBook = {Impulse: [JSON.parse(JSON.stringify(ui.normalizePosition(partial, 'Impulse', {})))]};
ui.st.portfolios = {portfolios: [{name: 'Impulse', positions: [partial]}]};
ui.renderPortfolios();
assert.match(elements.positions.innerHTML, /TP1 ✓ исполнен/);
assert.match(elements.positions.innerHTML, /TP1 · частично исполнен/);
assert.match(elements.positions.innerHTML, /Итог сделки 33/);
assert.match(elements.positions.innerHTML, /Зафиксировано до издержек/);
assert.match(elements.positions.innerHTML, /Переоценка остатка/);
assert.match(elements.positions.innerHTML, /Фондирование/);
ui.st.trades = {trades: [{...partial, status: 'CLOSED', net_pnl_rub: -75,
  total_trade_pnl_rub: -75, total_trade_return_pct: -.0075,
  avg_exit_price: 110, gross_pnl_rub: 120, fees_rub: 192, funding_rub: 3,
  exit_reason: 'TAKE_PROFIT_FULL_MIN_POSITION_R17'}]};
ui.renderTrades();
assert.match(elements.trades.innerHTML, /-0.01% NAV/);
assert.doesNotMatch(elements.trades.innerHTML, /10.00%/);
assert.match(elements.trades.innerHTML, /Фиксация по тейку/);
assert.match(elements.trades.innerHTML, /TP1 · частично исполнен/);
console.log('Partial take-profit and whole-trade UI regressions passed');
