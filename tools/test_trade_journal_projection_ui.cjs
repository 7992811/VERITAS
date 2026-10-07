// stdin: {pairs:[{original: fullApiTrade, projected: projectedApiTrade}, ...]}.
// Both rows must come from the SQL/API path; this harness does no projection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
assert.ok(Array.isArray(input.pairs) && input.pairs.length, 'Expected nonempty SQL row pairs');
const source = fs.readFileSync(path.join(__dirname, '..', 'veritas_v90_ui.py'), 'utf8');
const script = source.split('<script>')[1]?.split('</script>')[0];
assert.ok(script && /\}\)\(\);\s*$/.test(script), 'Canonical dashboard script not found');
const exposed = script.replace(/\}\)\(\);\s*$/,
  'globalThis.ui={st,renderTrades,buildFallbackIntelligence,positionSourceText};})();');

function render(row, expanded) {
  // Same offline document stub as tools/test_paper_ui.cjs. Startup never runs.
  const elements = {};
  const context = vm.createContext({
    document: {
      readyState: 'loading', addEventListener() {}, querySelectorAll: () => [],
      getElementById: id => elements[id] ||= {},
    },
  });
  vm.runInContext(exposed, context, {timeout: 2000});
  const ui = context.ui;
  ui.st.trades = {status: 'OK', trades: [row]};
  const key = String(row.trade_id || [row.portfolio_name, row.asset, row.opened_at, row.closed_at].join('|'));
  ui.st.expandedTrades = new Set(expanded ? [key] : []);
  ui.renderTrades();
  assert.ok(elements.trades.innerHTML.includes('<article class="deal">'), 'Trade fixture did not render');
  return {
    html: elements.trades.innerHTML,
    filters: elements.tradeFilters.innerHTML,
    intelligence: JSON.stringify(ui.buildFallbackIntelligence(null, null, null)),
    sourceLabel: ui.positionSourceText(row),
  };
}

function same(original, projected, label) {
  if (original === projected) return;
  let at = 0;
  while (at < Math.min(original.length, projected.length) && original[at] === projected[at]) at++;
  // Never dump complete SQL payloads or the dashboard source on a mismatch.
  const fragment = value => JSON.stringify(value.slice(Math.max(0, at - 60), at + 120));
  throw new Error(`${label} differs at ${at}: original=${fragment(original)} projected=${fragment(projected)}`);
}

let tp1 = 0, tp2 = 0, brent = 0;
input.pairs.forEach((pair, index) => {
  assert.ok(pair.original && pair.projected, `Pair ${index} needs original/projected API rows`);
  for (const expanded of [false, true]) {
    const original = render(pair.original, expanded);
    const projected = render(pair.projected, expanded);
    for (const field of ['html', 'filters', 'intelligence', 'sourceLabel']) {
      same(original[field], projected[field], `Pair ${index} ${field} expanded=${expanded}`);
    }
    if (!expanded) {
      if (original.html.includes('TP1 · частично исполнен')) tp1++;
      if (original.html.includes('TP2 · исполнен полностью')) tp2++;
      if (pair.original.asset === 'BRENT') brent++;
    }
  }
});
console.log(`PASS: ${input.pairs.length} SQL trade pairs preserve closed/expanded HTML, filters, intelligence and source labels (TP1=${tp1}, TP2=${tp2}, Brent=${brent})`);
