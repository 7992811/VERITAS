"""Canonical VERITAS Markets v9.0 UI.

Single-owner dashboard with full decision, portfolio, trade, learning and data-quality views.
No legacy DOM patching or duplicate network loaders.
"""
UI_VERSION = "veritas-ui-v9.0-r71-position-returns"

_CANONICAL_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VERITAS Markets</title>
<style>
:root{--bg:#0b1015;--card:#111820;--card2:#0e151c;--line:#26323c;--text:#e8edf2;--muted:#8b97a2;--ok:#59d694;--bad:#ef6767;--warn:#d6b75e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 Inter,Arial,sans-serif}
.wrap{max-width:1560px;margin:auto;padding:14px}
.brand-hero{width:100%;display:flex;align-items:center;justify-content:center;padding:7px 0 9px;margin-bottom:7px;border-bottom:1px solid rgba(255,255,255,.06);overflow:hidden}
.brand-logo{display:block;width:min(100%,1120px);height:auto;max-height:116px;object-fit:contain;object-position:center}
.status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin:0 0 10px}
.pill{border:1px solid var(--line);border-radius:9px;padding:7px 9px;font-size:10px;color:var(--muted);text-align:center;background:rgba(255,255,255,.015)}
.ok{color:#54e6a1!important}.bad{color:#ff6c75!important}.warn{color:#e8c55c!important}
.grid{display:grid;grid-template-columns:1.1fr 1.1fr .9fr;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;min-width:0;overflow:hidden}
.title{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:7px}
.full{grid-column:1/-1}.two{grid-column:span 2}.section{margin-top:8px}.msg{font-size:10px;color:var(--muted);padding:5px 0}
.row{display:grid;gap:6px;align-items:center;border-top:1px solid rgba(255,255,255,.045);padding:5px 0;min-width:0}.row:first-child{border-top:0}
.row b{font-size:11px}.row span{font-size:9px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.action{display:block;min-width:0;padding:8px 0}
.action-head{display:flex;flex-wrap:wrap;align-items:center;gap:5px 10px;min-width:0}
.action .action-reason,.action .action-meta{display:block;min-width:0;max-width:100%;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
.action .action-reason{margin-top:4px;font-size:10px;line-height:1.45;color:#d6dfe6}
.action .action-meta{margin-top:3px;font-size:9px;line-height:1.4}
.action-link{border:0;background:none;color:#b8d7ee;padding:0;font:inherit;cursor:pointer;text-align:left;text-decoration:underline;text-underline-offset:3px}
.execution-list{display:grid;gap:5px;margin-top:7px}.execution-item{font-size:10px;line-height:1.45;overflow-wrap:anywhere}.execution-item b{color:#eef3f7}
.action-box,.detail-text,.signal-chip{overflow-wrap:anywhere;max-width:100%;white-space:normal}
.asset{grid-template-columns:minmax(112px,142px) 82px 72px minmax(0,1fr);gap:5px;align-items:center;width:100%}
.asset-main{min-width:0;display:flex;align-items:center;gap:8px}.asset-main b{display:inline-block}
.asset-price{display:block!important;font-size:10px!important;color:#d2dbe3!important;font-variant-numeric:tabular-nums;text-align:left;padding-right:0}
.asset-bias{text-align:left;white-space:nowrap}
.asset-tfline{min-width:0;display:grid!important;grid-template-columns:repeat(7,minmax(0,1fr));gap:2px;overflow:hidden!important;white-space:normal!important;text-overflow:clip!important}
.asset-tfitem{display:block!important;min-width:0;padding:3px 1px;border:1px solid rgba(255,255,255,.04);border-radius:5px;background:rgba(255,255,255,.01);font-size:7.5px!important;color:#aeb9c3!important;text-align:center;white-space:nowrap!important;overflow:hidden;text-overflow:clip}
.tfs{display:grid;grid-template-columns:repeat(7,minmax(33px,1fr));gap:3px}.tf{font-size:7px;text-align:center;padding:3px 2px;border:1px solid var(--line);border-radius:5px;color:var(--muted)}
.matrix-wrap{overflow-x:hidden;overflow-y:visible;padding:2px 0 3px}
.matrix{width:100%;table-layout:fixed;border-collapse:separate;border-spacing:10px 9px}
.matrix th{font-size:12px;color:#c0cbd4;font-weight:650;padding:3px 2px;line-height:1.05;text-align:center}
.matrix th:first-child{width:132px}.matrix th.asset-head{text-align:left;width:132px;min-width:0;padding-left:0}.matrix td{padding:0;min-width:0;width:auto}
.asset-label{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:720;color:#edf2f6;white-space:nowrap}
.asset-logo{width:28px;height:28px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.24);background:#1a2530;color:#f2f6f9;font-size:11px;font-weight:850;box-shadow:0 0 0 1px rgba(255,255,255,.025),0 3px 9px rgba(0,0,0,.18);flex:0 0 28px}
.asset-logo svg{width:17px;height:17px;display:block;fill:currentColor;stroke:currentColor}
.asset-logo.btc{color:#ffb646;background:rgba(247,147,26,.12);border-color:rgba(255,182,70,.52)}
.asset-logo.eth{color:#c7b8ff;background:rgba(121,95,255,.10);border-color:rgba(199,184,255,.48)}
.asset-logo.ndxf{color:#78c1ff;background:rgba(39,145,224,.11);border-color:rgba(120,193,255,.48)}
.asset-logo.brent{color:#d2dbe4;background:rgba(205,219,230,.08);border-color:rgba(210,219,228,.38)}
.asset-logo.gold{color:#ffd45a;background:rgba(255,198,38,.11);border-color:rgba(255,212,90,.48)}
.asset-logo.moex{color:#8fc5ff;background:rgba(74,143,255,.10);border-color:rgba(143,197,255,.45)}
.asset-logo.cny{color:#ffd05d;background:rgba(221,67,67,.10);border-color:rgba(255,208,93,.45);font-size:10px}
.cell{width:100%;min-height:72px;border:1px solid rgba(255,255,255,.035);border-radius:9px;background:rgba(255,255,255,.012);color:var(--text);padding:5px 3px;cursor:pointer;font-size:11px;display:flex;flex-direction:column;align-items:center;justify-content:center;transition:.15s ease}
.cell:hover{background:rgba(255,255,255,.045);border-color:rgba(255,255,255,.10)}.cell.sel{outline:1px solid rgba(164,196,220,.62);outline-offset:1px;background:rgba(102,159,203,.055)}
.cell small{display:block;font-size:11px;color:#cfd5db;margin-top:5px;line-height:1.05;font-weight:520;white-space:nowrap}.cell em{display:block;font-style:normal;font-size:8.5px;color:#8f98a2;margin-top:4px;letter-spacing:.02em}
.sig-dot{display:inline-block;width:22px;height:22px;border-radius:50%;vertical-align:middle;position:relative}
.sig-dot.long{background:#52d98b;box-shadow:none}
.sig-dot.short{background:#ff5f6d;box-shadow:none}
.sig-dot.wait{background:#f6c451;box-shadow:none}
.sig-dot.super{width:32px;height:32px;background:transparent!important;border:3px solid currentColor;box-shadow:none}
.sig-dot.super::after{content:'';position:absolute;left:50%;top:50%;width:20px;height:20px;border-radius:50%;transform:translate(-50%,-50%);background:transparent!important;border:3px solid currentColor;box-shadow:none}
.sig-dot.super.long{color:#52d98b;border-color:#52d98b;box-shadow:none}
.sig-dot.super.short{color:#ff5f6d;border-color:#ff5f6d;box-shadow:none}
.super-label{font-weight:800;letter-spacing:.035em;color:#f3f7fa!important}
.signal-detail{max-width:1120px;margin:0 auto}
.signal-summary{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:11px 12px;border:1px solid var(--line);border-radius:11px;background:linear-gradient(120deg,rgba(255,255,255,.025),rgba(255,255,255,.008))}
.signal-identity{display:flex;align-items:center;gap:10px;min-width:180px}.signal-name{font-size:16px;font-weight:760}.signal-sub{font-size:9px;color:var(--muted);margin-top:2px}
.signal-chips{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.signal-chip{border:1px solid var(--line);border-radius:999px;padding:5px 8px;font-size:9px;color:#c8d1d8;background:#0e151c;white-space:nowrap}
.detail-columns{display:grid;grid-template-columns:1.12fr .88fr;gap:8px;margin-top:8px}
.detail-panel{border:1px solid var(--line);border-radius:10px;padding:9px;background:var(--card2);min-width:0}
.detail-panel h4{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}
.detail-text{font-size:10px;line-height:1.55;color:#cdd6dd}.detail-text b{color:#eef3f7}
.detail-list{display:grid;gap:5px}.detail-line{display:grid;grid-template-columns:128px minmax(0,1fr);gap:8px;font-size:9.5px;align-items:start}.detail-line span{color:var(--muted)}.detail-line b{color:#e9eef2;font-weight:620;overflow-wrap:anywhere}
.plan-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px}.plan-metric{border:1px solid rgba(255,255,255,.05);border-radius:8px;padding:6px;min-width:0}.plan-metric span{display:block;font-size:8px;color:var(--muted)}.plan-metric b{display:block;font-size:11px;margin-top:2px;white-space:normal;overflow-wrap:anywhere}
.action-box{margin-top:7px;border-left:3px solid #6ea7d0;background:rgba(110,167,208,.06);padding:7px 8px;border-radius:0 8px 8px 0;font-size:9.5px;line-height:1.5;color:#d6dfe6}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}.kpi{border:1px solid var(--line);border-radius:8px;padding:6px}.kpi span{display:block;color:var(--muted);font-size:8px}.kpi b{display:block;margin-top:2px;font-size:12px}
.portfolio-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:7px}.portfolio-card{border:1px solid var(--line);border-radius:10px;padding:9px;background:var(--card2);min-width:0}.portfolio-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}.portfolio-head b{font-size:12px}.portfolio-nav{font-size:11px;color:#dce4ea;font-weight:650}.portfolio-sub{font-size:9px;color:var(--muted);margin-top:2px}.portfolio-metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:5px}.portfolio-metric{border-top:1px solid rgba(255,255,255,.045);padding-top:5px}.portfolio-metric span{display:block;font-size:8px;color:var(--muted)}.portfolio-metric b{display:block;font-size:10px;margin-top:1px}
.position-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}
.position-card{border:1px solid var(--line);border-radius:7px;padding:4px 6px;background:var(--card2);min-width:0;overflow:hidden}
.position-card .asset-logo{width:20px;height:20px;flex-basis:20px;font-size:8.5px}
.position-card .asset-logo svg{width:13px;height:13px}
.position-head{display:flex;align-items:center;justify-content:space-between;gap:5px;min-height:20px}.position-head-main{display:flex;align-items:center;gap:4px;min-width:0}.position-head b{font-size:9.2px;line-height:1.08}.position-result{font-size:9.4px;line-height:1.05;font-weight:780;white-space:nowrap}
.position-meta{font-size:6.8px;line-height:1.12;color:var(--muted);margin-top:2px;white-space:normal;overflow-wrap:anywhere}
.position-levels{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:2px;margin-top:2px}
.position-level{min-width:0;border-top:1px solid rgba(255,255,255,.055);padding-top:2px}.position-level span{display:block;font-size:6.2px;line-height:1.05;color:var(--muted);white-space:nowrap}.position-level b{display:block;font-size:8.4px;line-height:1.08;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.position-learning{display:flex;flex-wrap:wrap;gap:2px;margin-top:2px}
.position-chip{border:1px solid rgba(255,255,255,.055);border-radius:999px;padding:1px 3px;font-size:6.2px;line-height:1.2;color:#aebac3;background:rgba(255,255,255,.01);white-space:nowrap}
.position-chip b{font-size:6.5px;color:#e4ebf0;font-weight:650}
.position-size-top,.trade-size-top{color:#dce5ec;font-weight:700}
.trade-card{border-top:1px solid rgba(255,255,255,.05);padding:5px 0}.trade-card:first-child{border-top:0}.trade-head{display:flex;align-items:center;justify-content:space-between;gap:7px}.trade-head b{font-size:9.8px;line-height:1.15}.trade-result{font-size:9.8px;font-weight:700;white-space:nowrap}.trade-meta{font-size:7.7px;line-height:1.25;color:var(--muted);margin-top:1px;white-space:normal;overflow-wrap:anywhere}.trade-money{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px 6px;margin-top:4px}.trade-money span{font-size:7px;line-height:1.15;color:var(--muted);min-width:0}.trade-money b{display:block;font-size:8.2px;line-height:1.15;color:var(--text);margin-top:1px;white-space:normal;overflow-wrap:anywhere}

.intel-wrap{display:grid;grid-template-columns:165px minmax(0,1fr);gap:10px;align-items:stretch}
.intel-score{border:1px solid var(--line);border-radius:11px;background:linear-gradient(145deg,rgba(91,143,183,.08),rgba(255,255,255,.01));padding:10px;display:flex;flex-direction:column;justify-content:space-between;min-width:0}
.intel-score .label{font-size:8px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.intel-score .value{font-size:30px;font-weight:760;line-height:1;margin-top:5px}.intel-score .sub{font-size:8.5px;color:var(--muted);margin-top:5px}
.intel-main{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
.intel-compare{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;margin-top:6px}
.intel-compare-card{border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:6px;background:rgba(255,255,255,.012);min-width:0}
.intel-compare-card span{display:block;font-size:7.2px;color:var(--muted)}
.intel-compare-card b{display:block;font-size:10px;margin-top:2px;line-height:1.15}
.intel-compare-card em{display:block;font-size:6.8px;font-style:normal;color:var(--muted);margin-top:2px;line-height:1.2}
.intel-metric{border:1px solid var(--line);border-radius:9px;padding:7px;background:var(--card2);min-width:0}.intel-metric span{display:block;font-size:8px;color:var(--muted)}.intel-metric b{display:block;font-size:11px;margin-top:2px}.intel-bar{height:4px;border-radius:999px;background:#1b252e;margin-top:6px;overflow:hidden}.intel-fill{height:100%;border-radius:999px;background:#8eb8d7}
.intel-daily{display:grid;grid-template-columns:1.25fr repeat(4,minmax(0,1fr));gap:5px;margin-top:6px}
.intel-daily-stat{border:1px solid rgba(255,255,255,.055);border-radius:8px;padding:5px 6px;background:rgba(255,255,255,.012);min-width:0}
.intel-daily-stat span{display:block;font-size:7.3px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.intel-daily-stat b{display:block;font-size:10px;margin-top:1px;white-space:nowrap}
.intel-daily-stat em{display:block;font-size:6.9px;font-style:normal;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.intel-foot{grid-column:1/-1;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:6px;margin-top:5px}.intel-stat{font-size:8.5px;color:var(--muted);border-top:1px solid rgba(255,255,255,.045);padding-top:5px}.intel-stat b{display:block;color:#dce5ec;font-size:10px;margin-top:1px}
.insight-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.insight{border:1px solid var(--line);border-radius:8px;padding:7px;min-width:0}.insight h4{margin:0 0 5px;font-size:9px;color:var(--muted);font-weight:500;text-transform:uppercase}.insight div{font-size:9px;line-height:1.6}
.scroll{max-height:450px;overflow:auto;padding-right:2px}
@media(max-width:1050px){.asset{grid-template-columns:124px 78px 66px minmax(0,1fr);gap:4px}.intel-wrap{grid-template-columns:1fr}.intel-main{grid-template-columns:repeat(3,minmax(0,1fr))}.intel-daily{grid-template-columns:repeat(3,minmax(0,1fr))}.intel-compare{grid-template-columns:repeat(2,minmax(0,1fr))}.intel-foot{grid-template-columns:repeat(3,minmax(0,1fr))}.grid{grid-template-columns:1fr}.two,.full{grid-column:1}.action{grid-template-columns:70px 86px 40px 1fr}.action .sl,.action .tp{display:none}.portfolio-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.status{grid-template-columns:repeat(2,minmax(0,1fr))}.detail-columns{grid-template-columns:1fr}.position-grid{grid-template-columns:1fr}.position-levels{grid-template-columns:repeat(5,minmax(0,1fr))}.trade-money{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:650px){.wrap{padding:9px}.asset{grid-template-columns:100px 70px 58px minmax(0,1fr);gap:3px}.asset-logo{width:22px;height:22px;flex-basis:22px}.asset-main{gap:5px}.asset-main b{font-size:9px}.asset-price{font-size:8.5px!important;padding-right:2px}.asset-bias{font-size:9px}.asset-tfline{grid-template-columns:repeat(3,minmax(0,1fr));gap:2px}.asset-tfitem{font-size:7px!important;padding:2px 1px}.intel-main{grid-template-columns:repeat(2,minmax(0,1fr))}.intel-daily{grid-template-columns:repeat(2,minmax(0,1fr))}.intel-compare{grid-template-columns:1fr}.intel-foot{grid-template-columns:repeat(2,minmax(0,1fr))}.brand-logo{max-height:82px}.matrix{border-spacing:5px 8px}.matrix th:first-child{width:92px}.matrix th.asset-head{width:92px}.asset-label{gap:5px;font-size:10px}.asset-logo{width:22px;height:22px;flex-basis:22px;font-size:9px}.cell{min-height:52px}.cell small{font-size:8px}.signal-summary{align-items:flex-start;flex-direction:column}.signal-chips{justify-content:flex-start}.detail-line{grid-template-columns:104px minmax(0,1fr)}.plan-grid{grid-template-columns:1fr 1fr}.portfolio-grid{grid-template-columns:1fr}.position-levels{grid-template-columns:repeat(3,minmax(0,1fr))}.trade-money{grid-template-columns:repeat(2,minmax(0,1fr))}}
.position-accounting{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:3px;margin:2px 0 0;font-size:6.4px;line-height:1.08;color:var(--muted)}.position-accounting span{min-width:0}.position-accounting b{display:block;color:var(--text);font-size:7.8px;line-height:1.08;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tp-status{font-size:6.8px;line-height:1.2;margin:2px 0}.position-result{max-width:42%;text-align:right}@media(max-width:650px){.position-accounting{grid-template-columns:repeat(3,minmax(0,1fr))}.tp-status{font-size:7.2px}}

/* Performance, exposure and complete-ledger quality, with readable mobile rows. */
.pf-panel{font-variant-numeric:tabular-nums}.pf-caption{font-size:10px;color:var(--muted);margin:10px 0 6px;line-height:1.45}
.pf-compare{border:1px solid var(--line);border-radius:10px;overflow:hidden}.pf-row{display:grid;grid-template-columns:minmax(110px,1.4fr) repeat(3,minmax(0,1fr));gap:8px;align-items:center;padding:10px 12px;width:100%;text-align:right;box-sizing:border-box}
button.pf-row{border:0;border-top:1px solid var(--line);border-radius:0;background:var(--card2);color:var(--text);font:inherit;font-size:12px;cursor:pointer;min-height:46px}
.pf-row>:first-child{text-align:left}.pf-row.pf-colnames{color:var(--muted);font-size:10px;background:var(--card)}.pf-row[aria-pressed="true"]{background:#192937;box-shadow:inset 3px 0 #87b9d9}.pf-row:hover{background:#172530}.pf-row:focus-visible,.deal-filters select:focus-visible{outline:2px solid #87b9d9;outline-offset:-3px}
.pf-row small{font-size:9px;color:var(--muted);display:block;margin-top:3px}.pf-detail{margin-top:12px;border:1px solid var(--line);border-radius:12px;padding:14px;background:var(--card2)}
.pf-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.pf-heading h3{font-size:15px;margin:0 0 5px}.pf-amount{font-size:26px;line-height:1.2;font-weight:720;letter-spacing:-.03em}.pf-secondary{font-size:11px;color:var(--muted);margin-top:4px}.pf-status{font-size:10px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;text-align:right;line-height:1.4}
.pf-performance{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:15px;padding-top:12px;border-top:1px solid var(--line)}.pf-value{min-width:0}.pf-value span{display:block;font-size:10px;line-height:1.35;color:var(--muted)}.pf-value b{display:block;font-size:16px;font-weight:650;margin-top:4px;overflow-wrap:anywhere}.pf-value small{display:block;color:var(--muted);font-size:9px;margin-top:4px;line-height:1.4}
.pf-sections{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin-top:18px}.pf-section{min-width:0;border-top:1px solid var(--line);padding-top:11px}.pf-section h4{margin:0 0 11px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#b6c6d2}.pf-pair{display:flex;justify-content:space-between;gap:9px;font-size:11px;line-height:1.4;margin:8px 0}.pf-pair span{color:var(--muted)}.pf-pair b{text-align:right;font-weight:600;white-space:nowrap}.pf-meter{height:5px;background:#25313c;border-radius:3px;overflow:hidden;margin:6px 0 12px}.pf-meter i{display:block;height:100%;background:#87b9d9;border-radius:3px}.pf-meter.is-risk i{background:var(--warn)}.pf-meter.is-breach i{background:var(--bad)}.pf-quality{display:grid;grid-template-columns:1fr 1fr;gap:12px}.pf-quality .pf-value b{font-size:19px}.pf-foot{font-size:9px;color:var(--muted);line-height:1.5;margin-top:10px}
.deal-filters{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:9px}.deal-filters select{background:var(--card2);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:11px;min-height:36px;padding:5px 8px;max-width:100%}.deal-filters small{font-size:10px;color:var(--muted);margin-left:auto}
#trades{max-height:620px}.deal{border:1px solid var(--line);border-radius:9px;background:var(--card2);padding:11px 12px;margin-bottom:7px;font-variant-numeric:tabular-nums}.deal-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.deal-name{font-size:13px;font-weight:650;line-height:1.4}.deal-name .deal-side{font-size:11px}.deal-book{font-size:10px;color:var(--muted);margin-top:2px}.deal-result{text-align:right;flex-shrink:0;font-size:16px;font-weight:720;line-height:1.25}.deal-result small{display:block;font-size:10px;font-weight:500;margin-top:3px}.deal-path{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}.deal-point{border-top:1px solid var(--line);padding-top:7px;font-size:10px;color:var(--muted)}.deal-point b{display:inline-block;font-size:12px;color:var(--text);margin-left:5px}.deal-point time{display:block;font-size:9px;margin-top:3px}.deal-outcome{display:flex;justify-content:space-between;gap:10px;align-items:baseline;margin-top:9px;font-size:10px;line-height:1.4}.deal-outcome span{color:var(--muted);text-align:right;flex-shrink:0}.deal details{margin-top:8px;border-top:1px solid var(--line);padding-top:6px}.deal summary{cursor:pointer;font-size:10px;color:#9ebad0;min-height:24px;line-height:24px}.deal-breakdown{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:5px}.deal-breakdown span{font-size:9px;color:var(--muted)}.deal-breakdown b{display:block;font-size:11px;color:var(--text);margin-top:3px}.deal .tp-status{font-size:10px;margin-top:7px}
.pf-ledger{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:780px){.pf-sections{grid-template-columns:1fr 1fr}.pf-section:last-child{grid-column:1/-1}.pf-quality{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:500px){.pf-row{grid-template-columns:minmax(88px,1.25fr) repeat(3,minmax(0,1fr));gap:4px;padding:9px 7px}button.pf-row{font-size:11px}.pf-row.pf-colnames{font-size:9px}.pf-row small{font-size:8px}.pf-detail{padding:11px}.pf-heading h3{font-size:13px}.pf-amount{font-size:23px}.pf-status{font-size:9px;max-width:104px}.pf-performance{gap:7px}.pf-value b{font-size:14px}.pf-value span{font-size:9px}.pf-sections{grid-template-columns:1fr;gap:10px}.pf-section:last-child{grid-column:auto}.pf-quality,.pf-ledger{grid-template-columns:1fr 1fr}.pf-quality .pf-value b{font-size:18px}.deal{padding:10px}.deal-result{font-size:15px}.deal-name{font-size:12px}.deal-breakdown{grid-template-columns:repeat(2,minmax(0,1fr))}.deal-filters small{flex-basis:100%;margin:0}.deal-outcome{gap:7px}}
.trade-direction{display:inline-block;margin-left:7px}.position-result{font-size:12px;line-height:1.25}.position-result small{display:block;font-size:9px;font-weight:550;margin-top:2px}.deal-result small{font-size:11px}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand-hero">
    <img class="brand-logo" src="data:image/webp;base64,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" alt="VERITAS Markets">
  </div>
  <div class="status">
    <span class="pill" id="sys">СИСТЕМА · —</span>
    <span class="pill" id="db">БАЗА · —</span>
    <span class="pill" id="cells">ДАННЫЕ · —</span>
    <span class="pill" id="stamp">ОБНОВЛЕНО · —</span>
  </div>

  <div class="card full" style="margin-bottom:8px">
    <div class="title">Интеллект VERITAS</div>
    <div id="intelligence"><div class="msg">Загрузка уровня знаний и опыта…</div></div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="title">Что делать сейчас</div>
      <div id="actions"><div class="msg">Загрузка сигналов…</div></div>
    </div>

    <div class="card two">
      <div class="title">Общий взгляд по активам</div>
      <div id="assets"><div class="msg">Загрузка рынка…</div></div>
    </div>

    <div class="card full section">
      <div class="title" style="font-size:12px;font-weight:700;letter-spacing:.06em;margin-bottom:5px">Матрица сигналов</div>
      <div class="matrix-wrap"><table class="matrix"><thead><tr id="matrixHead"><th scope="col">Актив</th></tr></thead><tbody id="matrixBody"></tbody></table></div>
    </div>

    <div class="card full section">
      <div class="title">Разбор выбранного сигнала</div>
      <div id="detail"><div class="msg">Нажми любую ячейку матрицы.</div></div>
    </div>

    <div class="card full section">
      <div class="title">Портфели</div>
      <div class="kpis">
        <div class="kpi"><span>Портфелей</span><b id="pfCount">—</b></div>
        <div class="kpi"><span>Открытых позиций</span><b id="openCount">—</b></div>
        <div class="kpi"><span>Лучшая доходность</span><b id="bestRet">—</b></div>
        <div class="kpi"><span>Наибольшая текущая просадка</span><b id="maxDD">—</b></div>
      </div>
      <div id="portfolios" style="margin-top:6px"><div class="msg">Загрузка портфелей…</div></div>
    </div>

    <div class="card full section">
      <div class="title">Открытые позиции</div>
      <div id="positions"><div class="msg">Загрузка позиций…</div></div>
    </div>

    <div class="card full section">
      <div class="title">Закрытые сделки</div>
      <div id="tradeFilters"></div><div id="trades" class="scroll"><div class="msg">Загрузка журнала…</div></div>
    </div>

    <div class="card full section">
      <div class="title">Обучение · качество данных · горизонты · макро</div>
      <div class="insight-grid">
        <div class="insight"><h4>Обучение</h4><div id="learning">Загрузка…</div></div>
        <div class="insight"><h4>Качество данных</h4><div id="quality">Загрузка…</div></div>
        <div class="insight"><h4>Целостность горизонтов</h4><div id="horizon">Загрузка…</div></div>
        <div class="insight"><h4>Макро</h4><div id="macro">Загрузка…</div></div>
      </div>
    </div>
  </div>
</div>

<script>
(function(){
'use strict';
const AS=['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'], TF=['1m','5m','1h','4h','1d','3d','7d'];
const POS_CACHE_KEY='veritas_v90_position_book_r35';
const loadPositionCache=()=>{try{const x=JSON.parse(localStorage.getItem(POS_CACHE_KEY)||'null');if(x&&x.book&&Date.now()-Number(x.at||0)<86400000)return x.book}catch(e){}return {}};
const savePositionCache=book=>{try{localStorage.setItem(POS_CACHE_KEY,JSON.stringify({at:Date.now(),book}))}catch(e){}};
const initialPositionBook=loadPositionCache();
const st={signals:null,portfolios:null,positionBook:initialPositionBook,positionBookReady:Object.values(initialPositionBook).some(v=>Array.isArray(v)&&v.length>0),trades:null,health:null,learning:null,quality:null,horizon:null,macro:null,intelligence:null,busy:{},selected:null};
const $=id=>document.getElementById(id);
const esc=v=>String(v==null?'—':v).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const lab=a=>a==='NQ'?'NDXf':a==='CNYRUBF'?'CNYRUBf':a;
const assetIconClass=a=>({BTC:'btc',ETH:'eth',NQ:'ndxf',BRENT:'brent',GOLD:'gold',MOEX:'moex',CNYRUBF:'cny'}[a]||'');
const assetLogo=a=>{
  const body={
    BTC:'₿',
    ETH:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 6.7 12 12 15.1 17.3 12 12 2Zm0 14.2-5.3-3.1L12 22l5.3-8.9-5.3 3.1Z"/></svg>',
    NQ:'N',
    BRENT:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2s6 7.1 6 12a6 6 0 1 1-12 0c0-4.9 6-12 6-12Z"/></svg>',
    GOLD:'Au',
    MOEX:'M',
    CNYRUBF:'¥₽'
  }[a]||'•';
  return '<span class="asset-logo '+assetIconClass(a)+'">'+body+'</span>';
};
const dir=x=>String((x&&x.research_decision)||(x&&x.decision)||'NO_TRADE');
const tier=x=>String((x&&x.signal_tier)||dir(x));
const tierLabel=x=>tier(x)==='SUPER_LONG'?'Strong Long':tier(x)==='SUPER_SHORT'?'Strong Short':dir(x)==='LONG'?'Long':dir(x)==='SHORT'?'Short':'ЖДАТЬ';
const cls=d=>d==='LONG'?'ok':d==='SHORT'?'bad':'warn', ar=d=>d==='LONG'?'↑':d==='SHORT'?'↓':'→';
const directionLabel=(d,t)=>String(t||'')==='SUPER_LONG'?'Strong Long':String(t||'')==='SUPER_SHORT'?'Strong Short':String(d||'')==='LONG'?'Long':String(d||'')==='SHORT'?'Short':'Нет позиции';
const n=(v,d=2)=>{if(v==null||v==='')return'—';v=Number(v);return Number.isFinite(v)?v.toLocaleString('ru-RU',{maximumFractionDigits:d}):'—'};
const p2=v=>{if(v==null||v==='')return'—';v=Number(v);return Number.isFinite(v)?v.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2}):'—'};
const probPct=v=>{v=Number(v);if(!Number.isFinite(v))return null;return v<=1.5?100*v:v};
const probSourceRu=v=>String(v||'').includes('EMPIRICAL')?'эмпир.':String(v||'').includes('CURRENT')?'тек.':'модельн.';
const focusRu=v=>({ЗАЩИТА_ПРИБЫЛИ:'защита прибыли',УДЕРЖАНИЕ_ДВИЖЕНИЯ:'удержание движения',КАЧЕСТВО_ВХОДА:'качество входа'}[String(v||'')]||'наблюдение');
const rub=v=>{if(v==null||v==='')return'—';v=Number(v);return Number.isFinite(v)?v.toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽':'—'};
const pct=v=>{if(v==null||v==='')return'—';v=Number(v);return Number.isFinite(v)?v.toFixed(2)+'%':'—'};
const bool=v=>v===true?'ДА':v===false?'НЕТ':'—';
const planOf=x=>(x&&x.trade_plan)||{};
const knownNumber=v=>v==null||v===''?null:(Number.isFinite(Number(v))?Number(v):null);
const tradeTotal=z=>knownNumber(z.total_trade_pnl_rub??(z.status==='CLOSED'?z.net_pnl_rub:null));
const tradeReturn=z=>z.trade_return_basis==='ENTRY_NOTIONAL'?knownNumber(z.total_trade_return_pct):null;
const tpDone=z=>z.tp1_done??Boolean((z.payload||{}).r17_tp1_done||String(z.exit_reason||(z.payload||{}).exit_reason||'').startsWith('TAKE_PROFIT'));
const tpNotice=(z,open=false)=>{
  if(!tpDone(z))return '';
  const p=z.payload||{},at=z.tp1_at||p.r17_tp1_at||z.closed_at,partial=z.tp1_partial??Boolean(p.r17_tp1_done);
  return '<div class="tp-status ok">'+(partial?'TP1 · частично исполнен':'Тейк · исполнен')+(at?' · '+dateRu(at):'')+(open?' · остаток сопровождается стопом':'')+'</div>';
};
const planStatus=x=>{
  const p=planOf(x),econ=p.final_economics_gate||{},blocked=(short,reason)=>({ready:false,short,reason});
  if(!['LONG','SHORT'].includes(dir(x)))return blocked('','Направление не подтверждено');
  if(x.market_open===false)return blocked('сессия','Торговая сессия закрыта');
  if(x.source_gate_pass===false)return blocked('данные','Цена устарела или источник не прошёл проверку');
  if((x.paper_eligible??x.execution_eligible)!==true)return blocked('данные','Нет допуска данных для модельной сделки');
  const blockers=econ.blockers||x.final_gate_blockers||[];
  if(blockers.some(v=>String(v).startsWith('QUOTE_')))return blocked('цена',blockers.map(v=>({QUOTE_TIME_MISSING:'Нет времени котировки',QUOTE_TIME_FUTURE:'Время котировки некорректно',QUOTE_TOO_OLD_FOR_HORIZON:'Котировка устарела для выбранного периода'}[v]||'')).filter(Boolean).join('; '));
  if(econ.status==='BLOCK'||x.final_gate_status==='BLOCK')return blocked('R/R',[...new Set(blockers.map(reasonRu))].join('; ')||'Не пройдена проверка торгового плана');
  if(x.entry_quality==='INVALIDATED'||p.entry_quality==='INVALIDATED'||x.decision_stage==='INVALIDATED')return blocked('отмена','Условия входа утратили актуальность');
  if(x.plan_eligible===false||p.eligible===false||x.decision_stage==='WAIT_RISK_REWARD')return blocked('ожидание',reasonRu(x.plan_reason||p.reason));
  if((x.plan_eligible??p.eligible)!==true)return blocked('план','Допуск торгового плана ещё не подтверждён');
  return {ready:true,short:'допущен',reason:'Сигнал допущен для модельной сделки; размер и открытие определяет портфель'};
};
const portfolioDecisions=x=>((st.portfolios&&st.portfolios.portfolios)||[]).flatMap(p=>(p.admission_trace||[]).filter(t=>t.asset===x.asset&&t.direction===dir(x)).map(t=>({...t,portfolio:p.name})));
const traceReason=t=>{
  const e=t.execution||{};
  if(e.status==='EXECUTED')return'Ордер исполнен';
  if(e.status==='HELD')return reasonRu(e.reason);
  const reasons=e.status==='BLOCKED'?(e.blockers?.length?e.blockers:[e.reason]):(t.hard_veto?(t.profitability_blockers||t.economics_blockers||t.source_blockers||[t.reason]):[e.reason||'EXECUTION_PENDING']);
  let text=[...new Set(reasons.filter(Boolean).map(reasonRu))].join('; ');
  if(['EXECUTION_QUOTE_UNAVAILABLE','R66_EXECUTION_QUOTE_STALE'].includes(e.reason)&&e.quote_gate?.age_seconds!=null)text+=': возраст '+Math.max(0,Number(e.quote_gate.age_seconds)).toFixed(0)+' с, допустимо '+Number(e.quote_gate.max_age_seconds||300).toFixed(0)+' с';
  if(e.timing?.consumed_move_pct!=null)text+=': движение '+(100*Number(e.timing.consumed_move_pct)).toFixed(2)+'%, предел '+(100*Number(e.timing.late_entry_limit_pct)).toFixed(2)+'%';
  const event=e.trend_event||t.trend_event;if(event?.extension_atr!=null)text+=': исходный уровень '+Number(event.trigger_level).toLocaleString('ru-RU',{maximumFractionDigits:2})+', удаление '+Number(event.extension_atr).toFixed(2)+' ATR';
  return text||'Окончательное решение ещё не получено';
};
const paperStatus=x=>{
  const plan=planStatus(x);if(!plan.ready)return plan;
  const all=portfolioDecisions(x),exact=all.filter(t=>t.horizon===x.horizon);
  const checked=exact.filter(t=>{const at=Date.parse(t.execution?.checked_at||'');return Number.isFinite(at)&&Date.now()-at<=180000&&Date.now()-at>=-60000&&(!t.signal_observed_at||!x.market_observed_at||t.signal_observed_at===x.market_observed_at)});
  if(!checked.length){
    if(all.length&&!exact.length)return{ready:false,short:'другой ТФ',reason:'Для входа портфели выбрали другой период: '+[...new Set(all.map(t=>tfRu(t.horizon)))].join(', ')};
    return{ready:false,short:'проверка',reason:'Торговый план прошёл проверку; ожидается актуальное решение исполнителя'};
  }
  if(checked.some(t=>t.execution?.status==='EXECUTED'))return{ready:true,short:'исполнен',reason:checked.filter(t=>t.execution?.status==='EXECUTED').map(t=>t.portfolio).join(', ')+': ордер исполнен'};
  const held=checked.filter(t=>t.execution?.status==='HELD');
  if(held.length)return{ready:false,short:'позиция',reason:held.map(t=>t.portfolio).join(', ')+': '+traceReason(held[0])};
  return{ready:false,short:'ожидание',reason:[...new Set(checked.flatMap(t=>traceReason(t).split('; ')))].join('; ')};
};
const rrOf=x=>x?.expected_to_stop_ratio??planOf(x).expected_to_stop_ratio;
const stopOf=x=>x?.stop_price??planOf(x).stop_price;
const targetOf=x=>x?.target_price??planOf(x).target_price??planOf(x).take_price;
const dirRu=d=>directionLabel(d,null);
const tfShort=tf=>({'1m':'1м','5m':'5м','1h':'1ч','4h':'4ч','1d':'1д','3d':'3д','7d':'7д'}[tf]||tf||'—');
const tfRu=tf=>({'1m':'1 мин','5m':'5 мин','1h':'1 ч','4h':'4 ч','1d':'1 день','3d':'3 дня','7d':'7 дней'}[tf]||tf||'—');
const holdRu=s=>{if(s==null||s==='')return'—';s=Number(s);if(!Number.isFinite(s)||s<0)return'—';const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.max(0,Math.floor((s%3600)/60));return(d?d+' д ':'')+(h?h+' ч ':'')+(m+' мин')};
const dateRu=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const exitRu=r=>{r=String(r||'').toUpperCase();if(r.startsWith('TAKE_PROFIT'))return'Фиксация по тейку';const m={TAKE_PROFIT:'Цель достигнута',TP:'Цель достигнута',STOP:'Стоп',STOP_LOSS:'Стоп',SIGNAL_FLIP:'Смена сигнала',TIMEOUT:'Выход по времени',PROFIT_HARVEST:'Фиксация прибыли',MANUAL:'Ручное закрытие',HARD_THESIS_INVALIDATION:'Сценарий отменён',PRODUCTION_CANDIDATE_REBASE:'Перезапуск портфеля',LEGACY_KERNEL_REBASE:'Переход на новые правила',V84_CONFIRMED_DIRECTION_FLIP:'Подтверждена смена направления',STRUCTURE_BREAK_EXIT_TO_CASH:'Нарушена структура тренда',CLOSED:'Закрыта'};return m[r]||'Закрытие системой'};
const regimeRu=v=>{const k=String(v||'');const m={UPTREND_HIGH_VOL:'Восходящий тренд, высокая волатильность',UPTREND_MID_VOL:'Восходящий тренд, средняя волатильность',UPTREND_LOW_VOL:'Восходящий тренд, низкая волатильность',DOWNTREND_HIGH_VOL:'Нисходящий тренд, высокая волатильность',DOWNTREND_MID_VOL:'Нисходящий тренд, средняя волатильность',DOWNTREND_LOW_VOL:'Нисходящий тренд, низкая волатильность',RANGE_HIGH_VOL:'Боковой рынок, высокая волатильность',RANGE_MID_VOL:'Боковой рынок, средняя волатильность',RANGE_LOW_VOL:'Боковой рынок, низкая волатильность'};return m[k]||'Режим уточняется'};
const structureRu=v=>{const k=String(v||'');const m={CONFIRMED_TREND:'Тренд подтверждён',BUILDING_TREND:'Тренд формируется',NEUTRAL:'Нейтральная структура',EXIT_REVERSAL:'Структура развернулась'};return m[k]||'Структура уточняется'};
const qualityRu=v=>{const k=String(v||'');const m={FRESH_BREAKOUT:'Свежий пробой',CONFIRMED_TREND:'Подтверждённый тренд',WAIT_CONFIRMATION:'Ожидание подтверждения',NEUTRAL:'Нейтрально',INVALIDATED:'Сценарий отменён'};return m[k]||'Оценка формируется'};
const stageRu=v=>{const k=String(v||'');const m={EARLY_PROBE:'Ранний вход',WAIT_RISK_REWARD:'Ожидание лучшего соотношения потенциала и риска',WAIT:'Ожидание',READY:'Готов к действию',INVALIDATED:'Сценарий отменён'};return m[k]||'Стадия уточняется'};
const reasonRu=v=>{
  const k=String(v||'').toUpperCase();
  const exact={
    EXECUTION_QUOTE_UNAVAILABLE:'Нет свежей котировки для исполнения',
    EXECUTION_PENDING:'Ожидается окончательная проверка исполнения',
    EXECUTION_CONTROL_BLOCKED:'Ордер не прошёл дополнительный контроль исполнения',
    NO_NEW_ALLOCATION:'Новый объём не выделен правилами портфеля',
    TARGET_ALREADY_REACHED:'Позиция уже открыта; новый добор не требуется',
    ORDER_ALREADY_RECORDED:'Этот ордер уже учтён; повторное открытие запрещено',
    WAIT_LEVEL_BREAK:'Ожидается пробой уровня',
    PORTFOLIO_RISK_LIMIT:'Достигнут лимит риска портфеля',
    DIRECTION_FLIP_NOT_CONFIRMED:'Разворот позиции ещё не подтверждён',
    R66_EXECUTION_QUOTE_STALE:'Котировка устарела: вход ожидает свежую цену',
    R66_SENIOR_BREAK_NOT_HELD:'Старший уровень пробит, но ещё не удержан',
    R66_WAIT_RETEST:'Импульс уже прошёл: ожидается удержание уровня или ретест',
    R67_LOCAL_CONTEXT_REQUIRED:'Нет надёжных локальных свечей NQ: новый риск запрещён',
    R67_DIRECT_NQ_QUOTE_REQUIRED:'Ожидается свежая котировка NQ: расчёт через QQQ не подтверждает вход',
    R67_WAIT_LOCAL_BREAKOUT:'Ожидается пробой заранее определённого локального уровня',
    R67_STRUCTURAL_STOP_RISK_LIMIT:'Размер ограничен риском до основания импульса',
    R66_LOCAL_EVENT_OPPOSED:'Последний локальный пробой направлен против сигнала',
    R66_CLOSED_CONTEXT_STALE:'Ожидается обновление закрытых свечей',
    R69_LOCAL_CONTEXT_REQUIRED:'Нет локальной структуры для входа',
    R69_WAIT_LOCAL_BREAKOUT:'Ждём новый пробой уровня',
    R69_BREAKOUT_ACTIVITY_REQUIRED:'Пробой не подтверждён активностью',
    R69_MINUTE_DATA_STALE:'Минутные свечи устарели или недоступны',
    R69_SOURCE_RISK_OR_ECONOMICS:'Вход не прошёл проверку данных, риска или расходов',
    R68_LOCAL_CONTEXT_INCOMPLETE:'Недостаточно полных локальных свечей для проверки входа',
    R66_ADD_NEEDS_CONFIRMATION:'Добор ожидает новое подтверждение в прибыльной стороне',
    R66_ADD_INSUFFICIENT_ROOM:'До ближайшего старшего уровня недостаточно хода для добора',
    R66_ADD_RISK_LIMIT:'Добор превышает допустимый риск по стопу',
    RR_BELOW_FINAL_FLOOR:'Потенциал относительно риска ниже порога',
    NET_REWARD_RISK_BELOW_FLOOR:'Потенциал после расходов недостаточен относительно риска',
    TARGET_NOT_PROFITABLE_AFTER_COSTS:'Доход до цели не покрывает расходы',
    EXPECTED_MOVE_BELOW_COST_BUFFER:'Ожидаемое движение не покрывает издержки с запасом',
    STOP_MISSING:'Не задан защитный стоп',STOP_DISTANCE_INVALID:'Некорректное расстояние до стопа',
    INSUFFICIENT_INDEPENDENT_EVIDENCE:'Недостаточно независимых подтверждений',
    INSUFFICIENT_MULTI_TF_ALIGNMENT:'Недостаточно согласованных периодов',
    HORIZON_STRUCTURE_TOO_WEAK:'Структура тренда недостаточно сильная',
    LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL:'Ранний пробой пока не прошёл проверку ожидаемой эффективности',
    EARLY_BREAKOUT_IN_RANGE_REGIME:'Ранний пробой ещё не подтверждён выходом из бокового рынка',
    R56_SENIOR_BIAS_REQUIRES_ENTRY_TRIGGER:'Нет подтверждённой локальной точки входа',
    R65_LOCAL_ENTRY_TRIGGER_REQUIRED:'Старший прогноз требует локальной точки входа',
    R65_LOCAL_ENTRY_CONTEXT_REQUIRED:'Нет актуального пятиминутного контекста',
    R65_WAIT_LOCAL_REVERSAL:'Локальная структура развернулась против входа',
    R65_ENTRY_ORIGIN_MISSING:'Не определён исходный уровень движения',
    R59_FINAL_FILL_MOVE_MARGIN_TOO_LOW:'По цене исполнения ожидаемое движение недостаточно',
    R59_FINAL_FILL_RR_MARGIN_TOO_LOW:'По цене исполнения потенциал относительно риска ниже порога',
    PAPER_ONE_VALID_SOURCE:'Данные допущены: одного проверенного источника достаточно'
  };
  if(exact[k])return exact[k];
  if(!k)return'Причина уточняется';
  if(k.includes('NEGATIVE_VALIDATED_SETUP_EDGE')||k.includes('NEGATIVE_CONTEXT_EXPECTANCY'))return'Историческая проверка похожих сценариев показывает отрицательный результат';
  if(k.includes('LATE_ENTRY')||k.includes('NO_CHASE'))return'Движение уже прошло слишком далеко; ожидается откат или новый пробой';
  if(k.includes('QUOTE_')||k.includes('CONTEXT_STALE'))return'Котировка или локальный контекст устарели';
  if(k.includes('INVALIDATED'))return'Условия входа утратили актуальность';
  if(k.includes('REENTRY')||k.includes('ADD_')||k.includes('NO_NEW_EVENT'))return'Для повторного входа или добора требуется новое рыночное событие';
  if(k.includes('SOURCE_GATE')||k.includes('SOURCE_TIME_KILL_GATE'))return'Нет допуска источника данных или торгового времени';
  if(k.includes('RR_TOO_LOW')||k.includes('NET_REWARD_RISK'))return'Потенциал после расходов недостаточен относительно риска';
  if(k.includes('EXPECTED_MOVE')||k.includes('COST_TO_EDGE')||k.includes('COST_DRAG'))return'Ожидаемое движение недостаточно относительно издержек';
  if(k.includes('WAIT_CONFIRMATION')||k.includes('PROVISIONAL'))return'Ожидается подтверждение нового сценария';
  if(k.includes('WEAK_BREAKOUT'))return'Пробой недостаточно сильный';
  if(k.includes('TACTICAL_REVERSAL'))return'Подтверждён тактический разворот';
  if(k.includes('NO_DIRECTION'))return'Подтверждённого направления пока нет';
  if(k.includes('REGIME_CONFLICT'))return'Направление противоречит подтверждённому старшему тренду';
  return'Не пройдена дополнительная проверка структуры, сигнала или риска';
};
const setupRu=v=>{const k=String(v||'').toUpperCase();if(k.includes('CLIMAX_REVERSAL'))return'Разворот после истощения импульса';if(k.includes('BASE_BREAKOUT'))return'Пробой уровня с закреплением';if(k.includes('PULLBACK_CONTINUATION'))return'Продолжение тренда после отката';if(k.includes('STRUCTURAL_BREAKOUT'))return'Структурный пробой';if(k.includes('TACTICAL_REVERSAL'))return'Тактический разворот';if(k.includes('RANGE_RETEST'))return'Ретест границы диапазона';return'Комбинированный сигнал'};
const matrixStateRu=x=>x?paperStatus(x).short:'';



async function get(key,url,ms){
  if(st.busy[key])return null;st.busy[key]=true;
  const ctl=new AbortController(),tm=setTimeout(()=>ctl.abort(),ms);
  try{const r=await fetch(url,{cache:'no-store',signal:ctl.signal});if(!r.ok)throw new Error('HTTP '+r.status);return await r.json()}
  catch(e){return null}finally{clearTimeout(tm);st.busy[key]=false}
}

function renderHealth(){
  const h=st.health||{};
  $('sys').textContent=h.ok?'СИСТЕМА · ОНЛАЙН':'СИСТЕМА · ЗАПУСК';$('sys').className='pill '+(h.ok?'ok':'warn');
  $('db').textContent=h.bootstrap_ready?'БАЗА · ГОТОВА':'БАЗА · ЗАПУСК';$('db').className='pill '+(h.bootstrap_ready?'ok':'warn');
}

function signalMap(){const m={};((st.signals&&st.signals.signals)||[]).forEach(x=>{if(x&&x.asset&&x.horizon)m[x.asset+'|'+x.horizon]=x});return m}

function renderSignals(){
  const d=st.signals||{}, rows=Array.isArray(d.signals)?d.signals:[], map=signalMap();
  const expectedCells=AS.length*TF.length;
  $('matrixHead').innerHTML='<th scope="col">Актив</th>'+TF.map(tf=>'<th scope="col" data-timeframe="'+tf+'" title="'+tfRu(tf)+'">'+tfShort(tf)+'</th>').join('');
  $('cells').textContent='ДАННЫЕ · '+rows.length+'/'+expectedCells;$('cells').className='pill '+(rows.length>=expectedCells?'ok':rows.length?'warn':'bad');
  $('stamp').textContent='ОБНОВЛЕНО · '+(d.at?new Date(d.at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—');

  const rank=x=>{const D=dir(x),T=tier(x);if(!['LONG','SHORT'].includes(D))return-999;const superBoost=(T==='SUPER_LONG'||T==='SUPER_SHORT')?5:0;return superBoost+(x.plan_eligible===false?0:2)+4*Number(x.horizon_structure_score||0)+2*Number(x.confidence||0)+Math.min(Number(x.expected_to_stop_ratio||0),3)+.2*Number(x.independent_evidence_families||0)};
  const selectedKeys=new Set(((st.portfolios&&st.portfolios.portfolios)||[]).flatMap(p=>(p.admission_trace||[]).map(t=>t.asset+'|'+t.horizon)));
  const ranked=rows.filter(x=>['LONG','SHORT'].includes(dir(x))).sort((a,b)=>
    (Number(selectedKeys.has(b.asset+'|'+b.horizon))-Number(selectedKeys.has(a.asset+'|'+a.horizon)))||rank(b)-rank(a));
  const seen=new Set(),best=ranked.filter(x=>{if(seen.has(x.asset))return false;seen.add(x.asset);return true}).slice(0,7);
  $('actions').innerHTML=best.length?best.map(x=>{
    const rr=Number(rrOf(x)),admission=paperStatus(x),sig=directionLabel(dir(x),tier(x));
    const state=admission.ready?'ОРДЕР ИСПОЛНЕН':admission.short==='позиция'?'ПОЗИЦИЯ ОТКРЫТА':admission.short==='проверка'?'ПРОВЕРКА ВХОДА':'ВХОД ЗАБЛОКИРОВАН';
    return '<div class="row action"><div class="action-head"><button class="action-link" data-signal="'+esc(x.asset+'|'+x.horizon)+'"><b>'+lab(x.asset)+'</b></button><b class="'+cls(dir(x))+'">'+ar(dir(x))+' '+sig+'</b><span>'+tfRu(x.horizon)+'</span></div>'+
      '<span class="action-reason"><b>'+state+'</b> · '+esc(admission.reason)+'</span>'+
      '<span class="action-meta">Потенциал / риск '+(Number.isFinite(rr)?rr.toFixed(2):'—')+' · Стоп '+p2(stopOf(x))+' · Цель '+p2(targetOf(x))+'</span></div>';
  }).join(''):'<div class="msg">Направленных сигналов сейчас нет — ожидается подтверждение структуры.</div>';
  document.querySelectorAll('.action-link[data-signal]').forEach(b=>b.onclick=()=>selectSignal(b.dataset.signal,true));

  $('assets').innerHTML=AS.map(a=>{const xs=TF.map(tf=>map[a+'|'+tf]).filter(Boolean),ds=xs.map(dir),ln=ds.filter(x=>x==='LONG').length,sn=ds.filter(x=>x==='SHORT').length,D=ln>sn?'LONG':sn>ln?'SHORT':'WAIT',p=(map[a+'|5m']||xs[0]||{}).price;return'<div class="row asset"><div class="asset-main">'+assetLogo(a)+'<b>'+lab(a)+'</b></div><span class="asset-price">'+n(p,4)+'</span><b class="asset-bias '+cls(D)+'">'+ar(D)+' '+(D==='LONG'?'Long':D==='SHORT'?'Short':'ЖДАТЬ')+'</b><span class="asset-tfline">'+TF.map(tf=>{const x=map[a+'|'+tf],shortTf=tfShort(tf);return'<span class="asset-tfitem">'+shortTf+' '+(x?ar(dir(x)):'—')+'</span>'}).join('')+'</span></div>'}).join('');

  $('matrixBody').innerHTML=AS.map(a=>'<tr><th class="asset-head"><div class="asset-label">'+assetLogo(a)+'<span>'+lab(a)+'</span></div></th>'+TF.map(tf=>{const x=map[a+'|'+tf];if(!x)return'<td><button class="cell"><span class="sig-dot wait" style="opacity:.35"></span><small>—</small><em></em></button></td>';const D=dir(x),T=tier(x),conf=100*Number(x.confidence||0),isSuper=(T==='SUPER_LONG'||T==='SUPER_SHORT'),dc=D==='LONG'?'long':D==='SHORT'?'short':'wait',state=matrixStateRu(x);return'<td><button class="cell" data-k="'+a+'|'+tf+'" title="'+esc(tierLabel(x))+' · '+conf.toFixed(1)+'% · '+esc(paperStatus(x).reason)+'"><span class="sig-dot '+dc+(isSuper?' super':'')+'"></span><small>'+conf.toFixed(1)+'%</small><em>'+esc(state)+'</em></button></td>'}).join('')+'</tr>').join('');
  document.querySelectorAll('.cell[data-k]').forEach(b=>b.onclick=()=>selectSignal(b.dataset.k));
  if(!st.selected&&rows.length){const x=best[0]||rows[0];st.selected=x.asset+'|'+x.horizon}
  if(st.selected)selectSignal(st.selected,false);
}

function selectSignal(k,scroll=false){
  st.selected=k;document.querySelectorAll('.cell').forEach(x=>x.classList.toggle('sel',x.dataset.k===k));
  const x=signalMap()[k];if(!x){$('detail').innerHTML='<div class="msg">Нет данных.</div>';return}
  const plan=x.trade_plan||{}, sl=x.structural_levels||{}, hs=x.horizon_structure||{}, D=dir(x);
  const rr=Number(rrOf(x)), conf=x.confidence==null?null:100*Number(x.confidence), exp=x.expected_move_pct??plan.expected_move_pct;
  const stop=stopOf(x), target=targetOf(x), confirms=Number(x.independent_evidence_families||0);
  const hstate=x.horizon_structure_state||hs.state, hscore=x.horizon_structure_score??hs.score;
  const admission=paperStatus(x),ready=admission.ready;
  const setup=plan.setup||((plan.trend_transition||{}).setup)||'';
  const pfs=(st.portfolios&&st.portfolios.portfolios)||[];
  const openPositions=[];
  pfs.forEach(p=>(p.positions||[]).forEach(z=>{if(String(z.asset)===String(x.asset))openPositions.push({p:p.name,z})}));
  const openShare=openPositions.reduce((a,q)=>a+100*Number(q.z.target_fraction||0),0);
  const openText=openPositions.length?openPositions.map(q=>esc(q.p)+' '+dirRu(q.z.direction)+' '+pct(100*Number(q.z.target_fraction||0))).join(' · '):'Открытой позиции по активу сейчас нет';
  const directionText=D==='LONG'?'Преимущество покупателей':D==='SHORT'?'Преимущество продавцов':'Направление не подтверждено';
  const structureText=structureRu(hstate)+(Number.isFinite(Number(hscore))?' · сила структуры '+(100*Number(hscore)).toFixed(0)+'%':'');
  const signalText=ready?'Ордер исполнен в модельном портфеле.':admission.short==='позиция'?'Позиция сопровождается; новый вход не требуется.':admission.short==='проверка'?'Ожидается решение исполнителя.':'Новый модельный вход пока заблокирован.';
  const actionText=ready
    ? signalText+' Стоп располагается за подтверждённой локальной структурой, прибыль сопровождается защитным трейлинг-стопом. При развитии движения возможен добор без увеличения исходного денежного риска.'
    : signalText+' '+esc(admission.reason)+'.';
  const invalidation=D==='LONG'
    ? 'Сценарий ослабнет при потере локальной поддержки и будет отменён при подтверждённом пробое структурного минимума.'
    : D==='SHORT'
      ? 'Сценарий ослабнет при возврате выше локального сопротивления и будет отменён при подтверждённом пробое структурного максимума.'
      : 'Новый сценарий появится после подтверждения направления и достаточного потенциала движения.';
  const positionMetric=openPositions.length?(openShare.toFixed(0)+'% суммарно'):'Нет';
  $('detail').innerHTML=
    '<div class="signal-detail">'+
      '<div class="signal-summary">'+
        '<div class="signal-identity">'+assetLogo(x.asset)+'<div><div class="signal-name">'+lab(x.asset)+' · '+tfRu(x.horizon)+'</div><div class="signal-sub">'+regimeRu(x.regime)+'</div></div></div>'+
        '<div class="signal-chips">'+
          '<span class="signal-chip '+cls(D)+'">'+tierLabel(x)+'</span>'+
          '<span class="signal-chip">Уверенность '+(conf==null?'—':conf.toFixed(1)+'%')+'</span>'+
          '<span class="signal-chip">Подтверждений '+confirms+'</span>'+
          '<span class="signal-chip" title="'+esc(admission.reason)+'">'+(ready?'Ордер исполнен':'Вход: '+esc(admission.short))+'</span>'+
        '</div>'+
      '</div>'+
      '<div class="detail-columns">'+
        '<div>'+
          '<div class="detail-panel"><h4>Смысл сигнала</h4><div class="detail-text"><b>'+directionText+'.</b> '+structureText+'. '+qualityRu(x.entry_quality)+'. '+reasonRu(x.plan_reason||plan.reason||x.execution_reason)+'.</div></div>'+
          '<div class="detail-panel" style="margin-top:8px"><h4>Что учитывает система</h4><div class="detail-list">'+
            '<div class="detail-line"><span>Рыночный режим</span><b>'+regimeRu(x.regime)+'</b></div>'+
            '<div class="detail-line"><span>Состояние структуры</span><b>'+structureText+'</b></div>'+
            '<div class="detail-line"><span>Качество входа</span><b>'+qualityRu(x.entry_quality)+'</b></div>'+
            '<div class="detail-line"><span>Стадия решения</span><b>'+stageRu(x.decision_stage)+'</b></div>'+
            '<div class="detail-line"><span>Тип сценария</span><b>'+setupRu(setup)+'</b></div>'+
            '<div class="detail-line"><span>Позиция в портфелях</span><b>'+openText+'</b></div>'+
          '</div></div>'+
          '<div class="action-box"><b>Действие VERITAS:</b> '+actionText+'<br><b>Когда сценарий отменяется:</b> '+invalidation+'</div>'+
          '<div class="execution-list">'+portfolioDecisions(x).map(t=>'<div class="execution-item"><b>'+esc(t.portfolio)+' · '+tfRu(t.horizon)+'</b> · '+esc(traceReason(t))+(t.execution?.checked_at?' · проверено '+dateRu(t.execution.checked_at):' · ожидается обновление')+'</div>').join('')+'</div>'+
        '</div>'+
        '<div class="detail-panel"><h4>Торговый план и уровни</h4><div class="plan-grid">'+
          '<div class="plan-metric"><span>Текущая цена</span><b>'+n(x.price,4)+'</b></div>'+
          '<div class="plan-metric"><span>Доля в портфелях</span><b>'+positionMetric+'</b></div>'+
          '<div class="plan-metric"><span>Стоп</span><b>'+n(stop,4)+'</b></div>'+
          '<div class="plan-metric"><span>Ближайшая цель</span><b>'+n(target,4)+'</b></div>'+
          '<div class="plan-metric"><span>Потенциал / риск</span><b>'+(!Number.isFinite(rr)?'—':rr.toFixed(2))+'</b></div>'+
          '<div class="plan-metric"><span>Ожидаемое движение</span><b>'+(exp==null?'—':(100*Number(exp)).toFixed(2)+'%')+'</b></div>'+
          '<div class="plan-metric"><span>Поддержка</span><b>'+n(sl.support,4)+'</b></div>'+
          '<div class="plan-metric"><span>Сопротивление</span><b>'+n(sl.resistance,4)+'</b></div>'+
          '<div class="plan-metric"><span>Средняя 18</span><b>'+n(sl.sma18,4)+'</b></div>'+
          '<div class="plan-metric"><span>Средняя 50</span><b>'+n(sl.sma50,4)+'</b></div>'+
        '</div><div class="detail-text" style="margin-top:8px">Стоп для лонга располагается ниже последнего подтверждённого локального минимума, для шорта — выше последнего локального максимума. После движения в прибыль защитный стоп подтягивается только по новой подтверждённой структуре.</div></div>'+
      '</div>'+
    '</div>';
  if(scroll)$('detail').scrollIntoView({behavior:'smooth',block:'nearest'});
}


const portfolioName=name=>({Impulse:'Импульсный',Aggressive:'Агрессивный',Champion:'Чемпион',Challenger:'Челленджер'}[name]||name||'—');
const tone=value=>value==null?'':Number(value)>0?'ok':Number(value)<0?'bad':'';
const signedPct=value=>value==null?'—':(Number(value)>0?'+':'')+pct(value);
function portfolioView(p){
  const l=p.latest||{},risk=p.risk_governor||(l.payload||{}).risk_governor||{};
  const value=key=>knownNumber(p[key]??l[key]);
  const gross=value('gross_leverage'),net=value('net_exposure'),closed=knownNumber(p.closed_trades),pnl=value('closed_trade_pnl_rub');
  return {balance:value('nav_rub'),usd:value('nav_usd'),ret:value('total_return_pct'),dd:knownNumber(p.drawdown_pct??(l.drawdown!=null?100*Number(l.drawdown):null)),
    gross,net,risk,limit:knownNumber(risk.max_gross),ddLimit:risk.hard_drawdown_limit==null?null:100*Number(risk.hard_drawdown_limit),
    long:gross==null||net==null?null:Math.max(0,(gross+net)/2),short:gross==null||net==null?null:Math.max(0,(gross-net)/2),
    cash:value('cash_equivalent_fraction'),closed,wins:knownNumber(p.wins),winRate:p.win_rate==null?null:100*Number(p.win_rate),pnl,
    avg:knownNumber(p.avg_closed_trade_pnl_rub)??(closed>0&&pnl!=null?pnl/closed:null),excess:value('excess_vs_ruonia_pct')};
}
function renderPortfolioPanel(ps){
  const root=$('portfolios');root.className='pf-panel';
  if(!ps.length){root.innerHTML='<div class="msg warn">Портфели пока не получены.</div>';return;}
  if(!ps.some(p=>p.name===st.selectedPortfolio))st.selectedPortfolio=ps[0].name;
  const p=ps.find(p=>p.name===st.selectedPortfolio),v=portfolioView(p),open=(p.positions||[]).length;
  const pair=(label,value,c='')=>'<div class="pf-pair"><span>'+label+'</span><b class="'+c+'">'+value+'</b></div>';
  const metric=(label,value,c='',sub='')=>'<div class="pf-value"><span>'+label+'</span><b class="'+c+'">'+value+'</b>'+(sub?'<small>'+sub+'</small>':'')+'</div>';
  const meter=(value,limit,risk=false)=>value==null||!(limit>0)?'':'<div class="pf-meter '+(risk?(value>=limit?'is-breach':'is-risk'):'')+'" aria-hidden="true"><i style="width:'+Math.min(100,Math.max(0,100*value/limit)).toFixed(2)+'%"></i></div>';
  const mult=x=>x==null?'—':n(x,2)+'×';
  const status=v.risk.new_risk===false?'Новый риск запрещён':open>0?open+' поз. открыто':v.gross==null?'Данные обновляются':v.gross>.002?'Позиции синхронизируются':'Вне рынка';
  const pf=knownNumber(p.profit_factor),pfText=pf==null?(p.profit_factor_state==='NO_LOSSES'?'Без убытков':'—'):n(pf,2);
  root.innerHTML='<div class="pf-caption">С начала учёта · выберите портфель для подробностей</div><div class="pf-compare"><div class="pf-row pf-colnames"><span>Портфель</span><span>Доходность</span><span>Просадка</span><span>Прибыльных</span></div>'+ps.map(q=>{const a=portfolioView(q);return'<button type="button" class="pf-row" data-portfolio="'+esc(q.name)+'" aria-pressed="'+(q.name===p.name)+'"><span><b>'+esc(portfolioName(q.name))+'</b><small>'+esc(q.name)+'</small></span><b class="'+tone(a.ret)+'">'+signedPct(a.ret)+'</b><span>'+pct(a.dd)+'</span><span>'+(a.winRate==null?'—':n(a.winRate,1)+'%')+'</span></button>';}).join('')+'</div>'+
    '<div class="pf-detail"><div class="pf-heading"><div><h3>'+esc(portfolioName(p.name))+'</h3><div class="pf-amount">'+rub(v.balance)+'</div><div class="pf-secondary">'+(v.usd==null?'—':n(v.usd,0)+' $')+'</div></div><div class="pf-status '+(v.risk.new_risk===false?'warn':'')+'">'+status+'</div></div>'+
    '<div class="pf-performance">'+metric('Доходность',signedPct(v.ret),tone(v.ret),'С начала учёта')+metric('К RUONIA',signedPct(v.excess),tone(v.excess),'Относительно эталона')+metric('Закрытые сделки',rub(v.pnl),tone(v.pnl),'После всех расходов')+'</div>'+
    '<div class="pf-sections"><section class="pf-section"><h4>Риск и ограничения</h4>'+pair('Текущая просадка',pct(v.dd),v.dd>0?'warn':'')+meter(v.dd,v.ddLimit,true)+pair('Лимит просадки',pct(v.ddLimit))+pair('Загрузка / лимит',mult(v.gross)+' / '+mult(v.limit))+meter(v.gross,v.limit)+pair('Новые позиции',v.risk.new_risk===true?'Разрешены':v.risk.new_risk===false?'Заблокированы':'—')+'</section>'+
    '<section class="pf-section"><h4>Экспозиция</h4>'+pair('Длинные позиции',mult(v.long))+pair('Короткие позиции',mult(v.short))+pair('Чистая экспозиция',mult(v.net))+pair('Вне позиций',v.cash==null?'—':pct(100*v.cash))+'<div class="pf-foot">Объём позиций относительно размера портфеля. 1× = 100%. Показатель «Вне позиций» не учитывает требования к марже.</div></section>'+
    '<section class="pf-section"><h4>Качество сделок</h4><div class="pf-quality">'+metric('Прибыльных',v.winRate==null?'—':n(v.winRate,1)+'%','',v.closed==null?'Нет статистики':(v.wins??'—')+' из '+v.closed)+metric('Коэф. прибыли',pfText,pf==null?'':pf>=1?'ok':'bad','Прибыли / убытки')+metric('Средняя сделка',rub(v.avg),tone(v.avg))+metric('Прибыль / убыток',p.payoff_ratio==null?'—':n(p.payoff_ratio,2),'','Средние значения')+'</div></section></div>'+
    '<div class="pf-section" style="margin-top:14px"><h4>Результат всех закрытых сделок</h4><div class="pf-quality pf-ledger">'+metric('Доход от цены',rub(p.closed_gross_pnl_rub),tone(p.closed_gross_pnl_rub))+metric('Комиссии',rub(p.closed_fees_rub))+metric('Фондирование',rub(p.closed_funding_rub))+metric('Итог',rub(v.pnl),tone(v.pnl))+'</div><div class="pf-foot">Коэффициент прибыли и средняя сделка рассчитаны после комиссий и фондирования по всей истории закрытых сделок. Просадка показана от достигнутого пика до текущего значения.</div></div></div>';
  document.querySelectorAll('#portfolios [data-portfolio]').forEach(el=>{el.onclick=()=>{st.selectedPortfolio=el.dataset.portfolio;renderPortfolioPanel(ps);};});
}

function renderPortfolios(){
  const d=st.portfolios||{},raw=Array.isArray(d.portfolios)?d.portfolios:[],positions=[];
  const ps=raw.map(p=>{
    const name=String(p.name||''),book=st.positionBook||{};
    const cached=Array.isArray(book[name])?book[name]:[],live=Array.isArray(p.positions)?p.positions:[];
    const pos=cached.length?cached:live.length?live:(st.positionBookReady?cached:live);
    return Object.assign({},p,{positions:pos});
  });
  ps.forEach(p=>(p.positions||[]).forEach(z=>positions.push(Object.assign({portfolio:p.name},z))));
  const exposureMismatch=positions.length===0&&portfolioExposureNonZero(ps);
  $('pfCount').textContent=ps.length+'/4';$('openCount').textContent=exposureMismatch?'синхр.':positions.length;
  const rets=ps.map(p=>knownNumber(p.total_return_pct??(p.latest||{}).total_return_pct)).filter(v=>v!=null);
  const dds=ps.map(p=>Number(p.drawdown_pct!=null?p.drawdown_pct:(((p.latest||{}).drawdown!=null)?100*Number((p.latest||{}).drawdown):NaN))).filter(Number.isFinite);
  $('bestRet').textContent=rets.length?Math.max(...rets).toFixed(2)+'%':'—';$('maxDD').textContent=dds.length?Math.max(...dds).toFixed(2)+'%':'—';
  renderPortfolioPanel(ps);
  $('positions').className='position-grid';
  $('positions').innerHTML=positions.length?positions.map(z=>{
    const pnl=tradeTotal(z),ret=tradeReturn(z),frac=100*Number(z.target_fraction||0),util=Number(z.position_utilization_pct),held=z.held_seconds??z.holding_duration_seconds;
    const stop=z.trailing_stop??z.stop_price,tp1=z.take_price??z.initial_take_price,tp1Label=tpDone(z)?'TP1 ✓ исполнен':z.take_price!=null?'TP1 / цель':z.initial_take_price!=null?'Цель входа':'TP1 / цель',tp2=z.second_take_price,prob=probPct(z.signal_probability),mfe=Number(z.mfe_pct),mae=Number(z.mae_pct),cap=Number(z.live_capture_ratio),give=Number(z.live_giveback_pct),rr=Number(z.expected_to_stop_ratio),exp=Number(z.expected_move_pct);
    const tf=z.execution_timeframe||z.horizon,grade=z.setup_grade||(z.legacy_entry_recovered?'архив':'—'),tier=z.signal_tier||'',protection=z.net_profit_protection||{},protect=protection.version==='NET_STOP_AFTER_COSTS_V1'&&protection.state==='PROTECTED'&&Number(protection.net_at_stop_rub)>=0.01;
    const probText=prob==null?'—':prob.toFixed(1)+'% '+probSourceRu(z.probability_source);
    const capText=Number.isFinite(cap)?(100*cap).toFixed(0)+'%':'—';
    const mfeText=Number.isFinite(mfe)?(mfe>=0?'+':'')+mfe.toFixed(2)+'%':'—';
    const maeText=Number.isFinite(mae)?mae.toFixed(2)+'%':'—';
    const giveText=Number.isFinite(give)?give.toFixed(2)+'%':'—';
    const sideText=directionLabel(z.direction,tier),sideClass=cls(String(z.direction||'')); 
    return'<div class="position-card">'+
      '<div class="position-head"><div class="position-head-main">'+assetLogo(z.asset)+'<b>'+esc(z.portfolio)+' · '+lab(z.asset)+' <span class="trade-direction '+sideClass+'">'+sideText+'</span> · <span class="position-size-top">'+frac.toFixed(0)+'%</span></b></div><div class="position-result '+(pnl==null?'warn':pnl>=0?'ok':'bad')+'" title="Результат всей сделки после расходов / сумма фактических входов и доборов. Частичные закрытия не уменьшают базу процента.">'+signedPct(ret)+'<small>'+rub(pnl)+'</small></div></div>'+
      '<div class="position-levels"><div class="position-level"><span>Вход</span><b>'+p2(z.avg_entry_price)+'</b></div><div class="position-level"><span>Сейчас</span><b>'+p2(z.last_price)+'</b></div><div class="position-level"><span>Stop Loss</span><b>'+p2(stop)+'</b></div><div class="position-level"><span>'+tp1Label+'</span><b>'+p2(tp1)+'</b></div><div class="position-level"><span>TP2</span><b>'+p2(tp2)+'</b></div></div>'+
      '<div class="position-meta">'+tfRu(tf)+' · открыта '+dateRu(z.opened_at)+' · в позиции '+holdRu(held)+' · объём '+rub(z.notional_rub)+'</div>'+
      tpNotice(z,true)+
      '<div class="position-accounting"><span>Зафиксировано<b>'+rub(z.realized_gross_pnl_rub)+'</b></span><span>Переоценка<b>'+rub(z.unrealized_pnl_rub)+'</b></span><span>Комиссии<b>'+rub(z.trade_fees_rub)+'</b></span><span>Фондирование<b>'+rub(z.trade_funding_rub)+'</b></span><span>От максимума<b>'+(Number.isFinite(util)?util.toFixed(0)+'%':'—')+'</b></span></div>'+
      '<div class="position-learning">'+
        '<span class="position-chip">Вероятность <b>'+probText+'</b></span>'+
        '<span class="position-chip">MFE <b>'+mfeText+'</b></span>'+
        '<span class="position-chip">MAE <b>'+maeText+'</b></span>'+
        '<span class="position-chip">Захват <b>'+capText+'</b></span>'+
        '<span class="position-chip">Отдано <b>'+giveText+'</b></span>'+
        '<span class="position-chip">R/R <b>'+(Number.isFinite(rr)?rr.toFixed(2):'—')+'</b></span>'+
        '<span class="position-chip">Ожид. ход <b>'+(Number.isFinite(exp)?(100*exp).toFixed(2)+'%':'—')+'</b></span>'+
        '<span class="position-chip">Grade <b>'+esc(grade)+'</b></span>'+
        '<span class="position-chip">Защита <b>'+(protect?'после расходов':protection.state==='STOP_REACHED'?'стоп достигнут':protection.state==='COSTS_NOT_COVERED'?'нет чистой прибыли':'нет расчёта')+'</b></span>'+
        '<span class="position-chip" title="Расчётный итог всей сделки при выходе по текущему стопу: с фиксациями, комиссиями, начисленным фондированием и проскальзыванием. Разрыв цены может изменить исполнение.">По стопу после расходов <b class="'+(protection.net_at_stop_rub==null?'warn':Number(protection.net_at_stop_rub)>=0?'ok':'bad')+'">'+(protection.net_at_stop_rub==null?'—':n(protection.net_at_stop_rub,2)+' ₽')+'</b></span>'+
        '<span class="position-chip" title="Расчётная цена стопа для нулевого итога всей сделки после расходов на текущий момент.">Безубыток <b>'+n(protection.break_even_stop_price,4)+'</b></span>'+
        '<span class="position-chip">Фокус <b>'+focusRu(z.learning_focus)+'</b></span>'+
      '</div>'+
    '</div>';
  }).join(''):(exposureMismatch?'<div class="msg warn">Экспозиция есть — позиции синхронизируются с PostgreSQL…</div>':'<div class="msg">Открытых позиций нет.</div>');
}

function renderTrades(){
  const all=Array.isArray(st.trades&&st.trades.trades)?st.trades.trades.slice(0,80):[];
  st.tradeFilter=st.tradeFilter||{portfolio:'',result:''};
  st.expandedTrades=st.expandedTrades||new Set();
  const f=st.tradeFilter,books=[...new Set([...(st.portfolios?.portfolios||[]).map(p=>p.name),...all.map(t=>t.portfolio_name)])].filter(Boolean);
  const selected=(a,b)=>a===b?' selected':'';
  const rows=all.filter(t=>(!f.portfolio||t.portfolio_name===f.portfolio)&&(!f.result||(tradeTotal(t)!=null&&(f.result==='win'?tradeTotal(t)>0:tradeTotal(t)<0))));
  $('tradeFilters').innerHTML='<div class="deal-filters"><select id="tradePortfolio" aria-label="Портфель закрытых сделок"><option value="">Все портфели</option>'+books.map(b=>'<option value="'+esc(b)+'"'+selected(f.portfolio,b)+'>'+esc(portfolioName(b))+'</option>').join('')+'</select><select id="tradeResult" aria-label="Результат закрытых сделок"><option value="">Любой результат</option><option value="win"'+selected(f.result,'win')+'>Прибыльные</option><option value="loss"'+selected(f.result,'loss')+'>Убыточные</option></select><small>'+rows.length+' из '+all.length+' последних сделок</small></div>';
  $('tradePortfolio').onchange=e=>{f.portfolio=e.target.value;renderTrades();};
  $('tradeResult').onchange=e=>{f.result=e.target.value;renderTrades();};
  $('trades').innerHTML=rows.length?rows.map(t=>{
    const net=tradeTotal(t),p=t.payload||{},entry=knownNumber(t.avg_entry_price),exit=knownNumber(t.avg_exit_price),D=String(t.direction||'');
    const heldRaw=t.holding_duration_seconds??t.held_seconds,held=heldRaw!=null?Number(heldRaw):(t.opened_at&&t.closed_at?Math.max(0,(new Date(t.closed_at)-new Date(t.opened_at))/1000):null);
    const retPct=tradeReturn(t),reason=exitRu(t.exit_reason||p.exit_reason||t.status);
    const openPct=knownNumber(t.opening_fraction_pct??(p.opening_fraction!=null?100*Number(p.opening_fraction):null));
    const maxPct=knownNumber(t.max_fraction_pct??(t.max_fraction!=null?100*Number(t.max_fraction):null));
    const shownPct=openPct??maxPct,sizeText=shownPct==null?'—':n(shownPct,0)+'%'+(shownPct>100?' · '+n(shownPct/100,2)+'×':'');
    const sizeLabel=openPct!=null?'При открытии':'Максимальный объём';
    const sideText=directionLabel(D,p.entry_signal_tier||p.signal_tier||''),sideClass=cls(D),stopLoss=t.stop_price??p.stop_price??p.structural_stop??p.initial_stop_price;
    const key=String(t.trade_id||[t.portfolio_name,t.asset,t.opened_at,t.closed_at].join('|'));
    return'<article class="deal"><div class="deal-head"><div><div class="deal-name">'+esc(lab(t.asset))+' <span class="deal-side trade-direction '+sideClass+'">'+sideText+'</span>'+(shownPct==null?'':' · <span>'+n(shownPct,0)+'%</span>')+'</div><div class="deal-book">'+esc(portfolioName(t.portfolio_name))+' · '+tfRu(t.horizon)+'</div></div><div class="deal-result '+tone(net)+'" title="Результат после расходов / сумма фактических входов и доборов.">'+signedPct(retPct)+'<small>'+rub(net)+'</small></div></div>'+
      '<div class="deal-path"><div class="deal-point">Вход <b>'+p2(entry)+'</b><time>'+dateRu(t.opened_at)+'</time></div><div class="deal-point">Выход <b>'+p2(exit)+'</b><time>'+dateRu(t.closed_at)+'</time></div></div><div class="deal-outcome"><b>'+esc(reason)+'</b><span>'+holdRu(held)+'</span></div>'+tpNotice(t)+
      '<details data-trade="'+esc(key)+'"'+(st.expandedTrades.has(key)?' open':'')+'><summary>Расчёт и параметры</summary><div class="deal-breakdown"><span>Доход от цены<b>'+rub(t.gross_pnl_rub)+'</b></span><span>Комиссии<b>'+rub(t.fees_rub)+'</b></span><span>Фондирование<b>'+rub(t.funding_rub)+'</b></span><span>Объём входов<b>'+rub(t.trade_return_basis_rub)+'</b></span><span>Stop Loss<b>'+p2(stopLoss)+'</b></span><span>'+sizeLabel+'<b>'+sizeText+'</b></span></div></details></article>';
  }).join(''):'<div class="msg">'+(all.length?'Нет сделок с выбранным фильтром.':'Закрытых сделок пока нет.')+'</div>';
  document.querySelectorAll('#trades details[data-trade]').forEach(el=>{el.ontoggle=()=>{if(el.open)st.expandedTrades.add(el.dataset.trade);else st.expandedTrades.delete(el.dataset.trade);};});
}

function deepNum(obj,names){
  const wanted=new Set(names.map(x=>String(x).toLowerCase()));
  const seen=new Set();
  function walk(v,depth){
    if(v==null||depth>6)return null;
    if(typeof v!=='object')return null;
    if(seen.has(v))return null;seen.add(v);
    for(const [k,val] of Object.entries(v)){
      if(wanted.has(String(k).toLowerCase())){
        const n=Number(val);if(Number.isFinite(n))return n;
      }
    }
    for(const val of Object.values(v)){
      const n=walk(val,depth+1);if(n!=null)return n;
    }
    return null;
  }
  return walk(obj,0);
}
function intelligenceRoot(raw){
  if(!raw||typeof raw!=='object')return {};
  return raw.intelligence_index||raw.intelligence_scorecard||raw.scorecard||raw.intelligence||raw.data||raw.result||raw;
}
function buildFallbackIntelligence(raw,progress,library){
  const trades=Array.isArray(st.trades&&st.trades.trades)?st.trades.trades:[];
  const closed=deepNum(progress,['clean_post_r2_closed_trades','clean_closed_trades','closed_episodes','closed_trades'])??Number((st.learning||{}).closed_trades||trades.length||0);
  const wins=deepNum(progress,['wins','profitable_trades','winning_trades'])??Number((st.learning||{}).wins||0);
  const wrRaw=deepNum(progress,['win_rate','clean_win_rate']);
  const wr=wrRaw!=null?(wrRaw>1?wrRaw/100:wrRaw):(closed>0?wins/closed:0);
  const sources=deepNum(library,['postgres_sources','knowledge_sources','source_count','sources'])??deepNum(raw,['postgres_sources','knowledge_sources','source_count']);
  const rules=deepNum(library,['postgres_rules','knowledge_rules','rule_count','rules'])??deepNum(raw,['postgres_rules','knowledge_rules','rule_count']);
  const principles=deepNum(library,['expert_principles','principles_count'])??deepNum(raw,['expert_principles','principles_count']);
  const layers=deepNum(progress,['core_learning_layers','learning_layers'])??16;
  let captureVals=[],covered=0;
  trades.forEach(t=>{const p=t.payload||{},mfe=Number(p.mfe_pct??t.mfe_pct),cap=Number(p.capture_ratio??t.capture_ratio);if(Number.isFinite(cap))captureVals.push(cap>1?cap/100:cap);else if(Number.isFinite(mfe)&&mfe>0){const en=Number(t.avg_entry_price||0),ex=Number(t.avg_exit_price||0),D=String(t.direction||'');if(en>0&&ex>0){const fav=D==='SHORT'?(en/ex-1):(ex/en-1);if(fav>0)captureVals.push(Math.max(0,Math.min(1,fav/(mfe/100))))}}if((p.mfe_pct??t.mfe_pct)!=null&&(p.mae_pct??t.mae_pct)!=null&&(p.exit_reason??t.exit_reason??t.status)!=null)covered++});
  const capture=captureVals.length?captureVals.reduce((a,b)=>a+b,0)/captureVals.length:0;
  const coverage=trades.length?covered/trades.length:0;
  const knowledge=10*Math.min(1,Number(sources||0)/100)+10*Math.min(1,Number(rules||0)/100);
  const experience=20*Math.min(1,Number(closed||0)/250);
  const outcome=25*Math.max(0,Math.min(1,(wr-0.20)/0.45));
  const captureScore=20*Math.max(0,Math.min(1,capture/0.70));
  const telemetry=15*Math.max(0,Math.min(1,coverage));
  const score=knowledge+experience+outcome+captureScore+telemetry;
  return {score,confidence:closed>=150?'HIGH':closed>=50?'MEDIUM':'LOW',components:{knowledge_breadth:knowledge,evidence_maturity:experience,outcome_quality:outcome,execution_capture_quality:captureScore,learning_telemetry_coverage:telemetry},knowledge:{expert_principles:principles,core_learning_layers:layers,sources,rules},evidence:{clean_post_r2_closed_trades:closed,win_rate:wr,avg_capture_ratio:capture,telemetry_coverage:coverage},core_learning_index:deepNum(raw,['learning_index','index_vs_start']),daily_progress:(raw&&raw.daily_progress)||{},derived_fallback:true};
}
function normalizeIntelligence(raw,progress,library){
  const ami=raw&&raw.asset_management_intelligence;
  if(ami&&ami.status==='OK'&&ami.score!=null){
    return {
      score:Number(ami.score),stage:String(ami.stage||''),confidence:String(ami.confidence||''),
      components:ami.components||{},component_maximums:ami.component_maximums||{},
      benchmarks:ami.benchmarks||{},evidence:ami.evidence||{},
      daily_progress:(raw&&raw.daily_progress)||{},core_learning_index:deepNum(raw,['learning_index','index_vs_start']),
      derived_fallback:false,real_asset_management_index:true,version:ami.version
    };
  }
  const root=intelligenceRoot(raw),c=root.components||root.component_scores||root.scores||{},k=root.knowledge||root.knowledge_base||{},e=root.evidence||root.experience||root.statistics||{};
  const score=deepNum(root,['score','intelligence_score','overall_score','maturity_score','maturity_index','intelligence_index']);
  if(score==null)return buildFallbackIntelligence(raw,progress,library);
  return {
    score:Number(score),confidence:String(root.confidence||root.confidence_level||''),
    components:{
      knowledge_breadth:deepNum(c,['knowledge_breadth','knowledge_score'])??0,
      evidence_maturity:deepNum(c,['evidence_maturity','experience_score','experience_maturity'])??0,
      outcome_quality:deepNum(c,['outcome_quality','result_quality','performance_quality'])??0,
      execution_capture_quality:deepNum(c,['execution_capture_quality','capture_quality','movement_capture'])??0,
      learning_telemetry_coverage:deepNum(c,['learning_telemetry_coverage','telemetry_coverage','learning_quality'])??0
    },
    knowledge:{
      expert_principles:deepNum(k,['expert_principles','principles_count'])??deepNum(library,['expert_principles','principles_count']),
      core_learning_layers:deepNum(k,['core_learning_layers','learning_layers'])??deepNum(progress,['core_learning_layers','learning_layers']),
      sources:deepNum(k,['sources','knowledge_sources','postgres_sources'])??deepNum(library,['sources','knowledge_sources','postgres_sources']),
      rules:deepNum(k,['rules','knowledge_rules','postgres_rules'])??deepNum(library,['rules','knowledge_rules','postgres_rules'])
    },
    evidence:{
      clean_post_r2_closed_trades:deepNum(e,['clean_post_r2_closed_trades','clean_closed_trades','closed_episodes','closed_trades'])??deepNum(progress,['clean_post_r2_closed_trades','closed_trades']),
      win_rate:deepNum(e,['win_rate','clean_win_rate'])??deepNum(progress,['win_rate','clean_win_rate']),
      avg_capture_ratio:deepNum(e,['avg_capture_ratio','average_capture_ratio','capture_ratio']),
      telemetry_coverage:deepNum(e,['telemetry_coverage','learning_telemetry_coverage'])
    },
    core_learning_index:deepNum(raw,['learning_index','index_vs_start'])??deepNum(progress,['index_vs_start']),
    daily_progress:(raw&&raw.daily_progress)||{},derived_fallback:false,real_asset_management_index:false
  };
}
function renderIntelligence(){
  const i=st.intelligence||{},c=i.components||{};
  if(i.score==null){$('intelligence').innerHTML='<div class="msg warn">Данные интеллекта временно не получены. Торговая система продолжает работать; повторная загрузка выполняется автоматически.</div>';return}
  const score=Number(i.score||0),conf={LOW:'низкая',MEDIUM:'средняя',HIGH:'высокая'}[String(i.confidence||'').toUpperCase()]||'формируется';
  const real=!!i.real_asset_management_index;
  const comp=real?[
    ['Решения рынка',Number(c.decision_intelligence||0),20],
    ['Результат портфелей',Number(c.portfolio_outcome_quality||0),25],
    ['Движение и риск',Number(c.movement_risk_management||0),15],
    ['Знания на практике',Number(c.knowledge_application||0),15],
    ['Независимый опыт',Number(c.experience_depth||0),10],
    ['Самообучение',Number(c.self_learning_effectiveness||0),15]
  ]:[
    ['Знания',Number(c.knowledge_breadth||0),20],
    ['Накопленный опыт',Number(c.evidence_maturity||0),20],
    ['Качество результата',Number(c.outcome_quality||0),25],
    ['Захват движения',Number(c.execution_capture_quality||0),20],
    ['Качество обучения',Number(c.learning_telemetry_coverage||0),15]
  ];
  const d=i.daily_progress||{},numOrNull=v=>v==null||v===''?null:(Number.isFinite(Number(v))?Number(v):null);
  const di=numOrNull(d.intelligence_delta_today??d.learning_index_delta_today);
  const trend=String(d.trend||'BUILDING'),trendText=trend==='UP'?'↑ качество растёт':trend==='DOWN'?'↓ качество снизилось':trend==='FLAT'?'→ без изменения':'накапливается';
  const trendClass=trend==='UP'?'ok':trend==='DOWN'?'bad':'warn',deltaText=di==null?'—':(di>0?'+':'')+di.toFixed(2)+' п.';
  const episodes=d.trade_learning_episodes_today==null?'—':d.trade_learning_episodes_today,outcomes=d.decision_outcomes_today==null?'—':d.decision_outcomes_today;
  const rulesToday=d.knowledge_rules_added_today==null?'—':d.knowledge_rules_added_today,sourcesToday=d.knowledge_sources_added_today==null?'—':d.knowledge_sources_added_today;
  let compare='';
  if(real){
    const b=i.benchmarks||{},iv=b.initial_veritas_decision_learning||{},sa=b.stateless_ai||{},rb=b.rollout_absolute_score||{};
    const ivDelta=iv.delta_points==null?'—':(Number(iv.delta_points)>=0?'+':'')+Number(iv.delta_points).toFixed(1)+' п.';
    const aiHit=sa.hit_rate_delta_pp==null?'—':(Number(sa.hit_rate_delta_pp)>=0?'+':'')+Number(sa.hit_rate_delta_pp).toFixed(1)+' п.п.';
    const aiCap=sa.large_move_capture_delta_pp==null?'—':(Number(sa.large_move_capture_delta_pp)>=0?'+':'')+Number(sa.large_move_capture_delta_pp).toFixed(1)+' п.п.';
    const roll=rb.delta_points==null?'0.0':(Number(rb.delta_points)>=0?'+':'')+Number(rb.delta_points).toFixed(1);
    const le=(i.evidence||{}).learning_episodes||{},badEarly=numOrNull(le.early_bad_rate),badRecent=numOrNull(le.recent_bad_rate),rEarly=numOrNull(le.early_realization),rRecent=numOrNull(le.recent_realization);
    const learnText=(badEarly!=null&&badRecent!=null)?(100*badEarly).toFixed(0)+'% → '+(100*badRecent).toFixed(0)+'% ошибок':'выборка формируется';
    const realText=(rEarly!=null&&rRecent!=null)?(100*rEarly).toFixed(0)+'% → '+(100*rRecent).toFixed(0)+'% реализации':'—';
    compare='<div class="intel-compare">'+
      '<div class="intel-compare-card"><span>К стартовому VERITAS</span><b class="'+(Number(iv.delta_points||0)>=0?'ok':'bad')+'">'+ivDelta+'</b><em>индекс решений: '+esc(iv.current??'—')+' при базе 100</em></div>'+
      '<div class="intel-compare-card"><span>К AI без памяти</span><b class="'+(Number(sa.hit_rate_delta_pp||0)>=0?'ok':'bad')+'">'+aiHit+' точность</b><em>'+aiCap+' захват · n='+esc(sa.sample_n??0)+' · '+esc(sa.status||'BUILDING')+'</em></div>'+
      '<div class="intel-compare-card"><span>Самообучение</span><b>'+learnText+'</b><em>реализация ожидаемого хода: '+realText+' · от запуска индекса '+roll+' п.</em></div>'+
    '</div>';
  }
  const daily='<div class="intel-daily">'+
    '<div class="intel-daily-stat"><span>Эффективность сегодня</span><b class="'+trendClass+'">'+deltaText+' · '+trendText+'</b><em>не объём знаний, а изменение качества решений</em></div>'+
    '<div class="intel-daily-stat"><span>Обучающих эпизодов</span><b>'+esc(episodes)+'</b><em>новых завершённых сделок</em></div>'+
    '<div class="intel-daily-stat"><span>Проверенных исходов</span><b>'+esc(outcomes)+'</b><em>решений с известным результатом</em></div>'+
    '<div class="intel-daily-stat"><span>Новых правил</span><b>+'+esc(rulesToday)+'</b><em>информативно, сами по себе балл почти не дают</em></div>'+
    '<div class="intel-daily-stat"><span>Новых источников</span><b>+'+esc(sourcesToday)+'</b><em>информативно, без доказанной пользы не повышают интеллект</em></div>'+
  '</div>';
  let foot='';
  if(real){
    const e=i.evidence||{},k=e.knowledge||{},p=e.fresh_candidate_trades||{},l=e.learning_episodes||{};
    foot='<div class="intel-foot">'+
      '<div class="intel-stat">Независимых эпизодов<b>'+esc(e.independent_decision_episodes??'—')+'</b></div>'+
      '<div class="intel-stat">Новая чистая эпоха<b>'+esc(p.n??0)+' сделок</b></div>'+
      '<div class="intel-stat">OOS-подтверждённых правил<b>'+esc(k.validated_oos_rules??0)+'</b></div>'+
      '<div class="intel-stat">Знания применены<b>'+((Number(k.application_rate)||0)*100).toFixed(1)+'%</b></div>'+
      '<div class="intel-stat">Эпизодов самообучения<b>'+esc(l.n??0)+'</b></div>'+
      '<div class="intel-stat">Средний захват движения<b>'+((Number(l.avg_capture_ratio)||0)*100).toFixed(1)+'%</b></div>'+
    '</div>';
  }else{
    const k=i.knowledge||{},e=i.evidence||{},wr=e.win_rate==null?'—':(100*Number(e.win_rate)).toFixed(1)+'%',capture=e.avg_capture_ratio==null?'—':(100*Number(e.avg_capture_ratio)).toFixed(1)+'%';
    foot='<div class="intel-foot"><div class="intel-stat">Источники знаний<b>'+esc(k.sources??'—')+'</b></div><div class="intel-stat">Правила / принципы<b>'+esc(k.rules??k.expert_principles??'—')+'</b></div><div class="intel-stat">Завершённых эпизодов<b>'+esc(e.clean_post_r2_closed_trades??'—')+'</b></div><div class="intel-stat">Win-rate выборки<b>'+wr+'</b></div><div class="intel-stat">Средний захват движения<b>'+capture+'</b></div></div>';
  }
  const title=real?'Прикладной интеллект управления активами':'Индекс зрелости системы';
  const explanation=real?'Не IQ: измеренная способность анализировать рынок, применять знания, управлять риском и улучшать решения на фактических исходах.':'Рост индекса отражает знания, опыт, качество решений и полноту обратной связи.';
  $('intelligence').innerHTML='<div class="intel-wrap"><div class="intel-score"><div><div class="label">'+title+'</div><div class="value">'+score.toFixed(1)+'</div><div class="sub">из 100 · уровень: '+esc(i.stage||'—')+' · доказательность: '+conf+'</div></div><div class="sub">'+explanation+'</div></div><div><div class="intel-main">'+comp.map(x=>'<div class="intel-metric"><span>'+x[0]+'</span><b>'+x[1].toFixed(1)+' / '+x[2]+'</b><div class="intel-bar"><div class="intel-fill" style="width:'+Math.max(0,Math.min(100,100*x[1]/x[2]))+'%"></div></div></div>').join('')+'</div>'+compare+daily+foot+'</div></div>';
}
function renderInsights(){
  const l=st.learning||{}, q=st.quality||{}, h=st.horizon||{}, m=st.macro||{}, ep=(st.portfolios||{}).episode_learning_r29||{};
  const wr=l.win_rate==null?'—':(100*Number(l.win_rate)).toFixed(1)+'%';
  const ac=ep.attribution_counts||{}, labels={ENTRY_DIRECTION_ERROR:'ошибка входа/направления',EDGE_OVERFORECAST:'переоценка ожидаемого хода',COST_DRAG:'издержки съели преимущество',EXIT_CAPTURE_ERROR:'потеря движения при сопровождении',STOP_STRUCTURE_ERROR:'ошибка стоп-структуры',GOOD_EXECUTION:'качественное исполнение',MIXED_EXECUTION:'смешанный результат'};
  let topKey=null,topN=-1;Object.entries(ac).forEach(([k,v])=>{const n=Number(v||0);if(n>topN){topN=n;topKey=k}});
  const epText=ep.eligible_episodes==null?'—':esc(ep.eligible_episodes),topText=topKey?(labels[topKey]||topKey)+' · '+topN:'—';
  $('learning').innerHTML='<b>Закрытых сделок:</b> '+esc(l.closed_trades??'—')+'<br><b>Прибыльных:</b> '+esc(l.wins??'—')+'<br><b>Win-rate:</b> '+wr+'<br><b>Эпизодов R29:</b> '+epText+'<br><b>Главный урок:</b> '+esc(topText)+'<br><b>Память опыта:</b> '+esc(l.experience_storage||'—');
  $('quality').innerHTML='<b>Матрица:</b> '+esc(q.cells??'—')+'/'+esc(q.expected_cells??42)+'<br><b>Источник подтверждён:</b> '+esc(q.source_verified_cells??'—')+' ячеек<br><b>Можно исполнять:</b> '+esc(q.execution_eligible_cells??'—')+' ячеек<br><b>Устаревших:</b> '+esc(q.stale_cells??'—');
  $('horizon').innerHTML=TF.map(tf=>'<b>'+tf+':</b> '+esc(h[tf]??0)+'/7 активов').join('<br>');
  const macroObj=m.macro||m, regime=m.regime||{};
  const macroLines=[];
  if(regime&&typeof regime==='object'){if(regime.regime)macroLines.push('<b>Режим:</b> '+esc(regime.regime));if(regime.summary)macroLines.push(esc(regime.summary))}
  ['dxy','vix','us10y','oil','gold'].forEach(k=>{if(macroObj&&macroObj[k]!=null)macroLines.push('<b>'+k.toUpperCase()+':</b> '+esc(macroObj[k]))});
  $('macro').innerHTML=macroLines.slice(0,6).join('<br>')||'Макро-контекст обновляется отдельно и не блокирует торговые данные.';
}

function mergePortfolioSets(primary,secondary,preferPrimaryPositions=false){
  const a=Array.isArray(primary&&primary.portfolios)?primary.portfolios:[],b=Array.isArray(secondary&&secondary.portfolios)?secondary.portfolios:[];
  const map=new Map();
  b.forEach(p=>map.set(String(p.name||''),Object.assign({},p)));
  a.forEach(p=>{
    const k=String(p.name||''),old=map.get(k)||{},hasPos=Array.isArray(p.positions);
    const merged=Object.assign({},old,p);
    if(preferPrimaryPositions){
      merged.positions=hasPos?p.positions:(Array.isArray(old.positions)?old.positions:[]);
    }else if(!hasPos&&Array.isArray(old.positions)){
      merged.positions=old.positions;
    }
    map.set(k,merged);
  });
  return Object.assign({},secondary||{},primary||{},{portfolios:Array.from(map.values())});
}
function portfolioExposureNonZero(ps){
  return (ps||[]).some(p=>{
    const g=Number(p&&((p.latest||{}).gross_leverage??p.gross_leverage)),n=Number(p&&((p.latest||{}).net_exposure??p.net_exposure));
    return (Number.isFinite(g)&&Math.abs(g)>0.002)||(Number.isFinite(n)&&Math.abs(n)>0.002);
  });
}
function normalizePosition(z,portfolioHint,d){
  if(!z||typeof z!=='object')return null;
  const p=Object.assign({},z.payload||{},z);
  const portfolio=String(p.portfolio_name||p.portfolio||portfolioHint||'');
  const asset=String(p.asset||p.symbol||'');
  const direction=String(p.direction||p.side||'').toUpperCase();
  if(!portfolio||!asset||!['LONG','SHORT'].includes(direction))return null;

  const trades=Array.isArray(d&&d.trades)?d.trades:(Array.isArray(d&&d.trades&&d.trades.trades)?d.trades.trades:[]);
  const tr=trades.find(t=>{
    const status=String(t.status||'').toUpperCase();
    return String(t.portfolio_name||t.portfolio||'')===portfolio&&String(t.asset||'')===asset&&(!status||['OPEN','ACTIVE'].includes(status));
  })||{};
  const tp=tr.payload||{};
  const sigs=Array.isArray(d&&d.signals)?d.signals:[];
  const sig=sigs.find(x=>String(x.asset||'')===asset&&String(x.horizon||'')==='5m')||sigs.find(x=>String(x.asset||'')===asset)||{};
  const plan=sig.trade_plan||{};

  const first=(...v)=>v.find(x=>x!==undefined&&x!==null&&x!=='');
  const num=(...v)=>{const x=Number(first(...v));return Number.isFinite(x)?x:null};

  return Object.assign({},tr,z,{
    portfolio_name:portfolio,
    portfolio:portfolio,
    asset:asset,
    direction:direction,
    avg_entry_price:num(z.avg_entry_price,z.entry_price,tr.avg_entry_price,tp.entry_price),
    last_price:num(z.last_price,z.price,sig.price,tr.last_price),
    stop_price:num(z.stop_price,z.trailing_stop,tp.trailing_stop,tp.stop_price,tp.initial_stop_price,plan.stop_price),
    target_fraction:num(z.target_fraction,z.current_fraction,tr.target_fraction,tp.target_fraction,tr.max_fraction,tp.opening_fraction),
    opened_at:first(z.opened_at,z.entry_time,tr.opened_at,tp.entry_time),
    horizon:first(z.horizon,z.execution_timeframe,tr.horizon,tp.execution_timeframe,sig.horizon),
    take_price:num(z.take_price,z.target_price,tp.take_price,tp.target_price,tp.last_target_price,plan.target_price),
    second_take_price:num(z.second_take_price,tp.tp2,tp.tp2_price,tp.second_target_price),
    signal_probability:first(z.signal_probability,tp.pwin,tp.entry_probability),
    probability_source:first(z.probability_source,tp.pwin_source,tp.probability_source),
    signal_tier:first(z.signal_tier,tp.entry_signal_tier,tp.signal_tier,sig.signal_tier),
    payload:Object.assign({},tp,z.payload||{})
  });
}
function extractPositionCandidates(d){
  const out=[];
  const push=(arr,hint)=>{(Array.isArray(arr)?arr:[]).forEach(z=>{const n=normalizePosition(z,hint,d);if(n)out.push(n)})};
  const ps=Array.isArray(d&&d.portfolios)?d.portfolios:[];
  ps.forEach(p=>push(p&&p.positions,String((p&&p.name)||'')));
  push(d&&d.open_positions,'');
  push(d&&d.positions,'');
  push(d&&d.portfolio_positions,'');
  push(d&&d.paper_positions,'');
  const t=Array.isArray(d&&d.trades)?d.trades:(Array.isArray(d&&d.trades&&d.trades.trades)?d.trades.trades:[]);
  t.filter(x=>['OPEN','ACTIVE'].includes(String((x&&x.status)||'').toUpperCase())).forEach(x=>{const n=normalizePosition(x,String(x.portfolio_name||x.portfolio||''),d);if(n)out.push(n)});
  const uniq=new Map();
  out.forEach(z=>uniq.set(String(z.portfolio_name||z.portfolio)+'|'+String(z.asset),Object.assign({},uniq.get(String(z.portfolio_name||z.portfolio)+'|'+String(z.asset))||{},z)));
  return Array.from(uniq.values());
}
function ingestExtractedPositions(d,{allowClear=false}={}){
  const rows=extractPositionCandidates(d);
  const ps=Array.isArray(d&&d.portfolios)?d.portfolios:[];
  if(rows.length){
    const next={};
    rows.forEach(z=>{const name=String(z.portfolio_name||z.portfolio||'');if(!next[name])next[name]=[];next[name].push(z)});
    ['Impulse','Aggressive','Champion','Challenger'].forEach(name=>{if(!next[name])next[name]=[]});
    st.positionBook=next;st.positionBookReady=true;savePositionCache(next);return true;
  }
  if(allowClear&&ps.length&&!portfolioExposureNonZero(ps)){
    const next={};ps.forEach(p=>{const name=String(p.name||'');if(name)next[name]=[]});
    st.positionBook=next;st.positionBookReady=true;savePositionCache(next);return true;
  }
  return false;
}
function ingestPositionBook(ps,{allowClear=false}={}){
  return ingestExtractedPositions({portfolios:Array.isArray(ps)?ps:[]},{allowClear});
}
function updatePositionBookFromBootstrap(d){
  ingestExtractedPositions(d,{allowClear:!!(d&&d.health&&d.health.bootstrap_ready)});
}
function applyBootstrap(d){
  if(!d)return;
  st.health={ok:true,bootstrap_ready:!!(d.health&&d.health.bootstrap_ready)};
  st.signals={signals:d.signals||[],at:d.at,status:d.status};
  updatePositionBookFromBootstrap(d);
  st.portfolios=mergePortfolioSets({portfolios:d.portfolios||[]},st.portfolios,false);
  st.trades=Array.isArray(d.trades)?{trades:d.trades}:(d.trades||{trades:[]});
  st.learning=d.learning_summary||{};
  st.quality=d.data_quality_summary||{};
  st.horizon=d.horizon_summary||{};
  if(d.intelligence_index)st.intelligence=normalizeIntelligence(d.intelligence_index,null,null);
  renderHealth();renderSignals();renderPortfolios();renderTrades();renderIntelligence();renderInsights();
}

async function loadBootstrap(){
  const d=await get('bootstrap','/api/v1/dashboard-bootstrap',8000);
  if(d)applyBootstrap(d);
}
async function loadPortfolios(){
  const d=await get('paper-portfolios','/api/v1/paper-portfolios',12000);
  if(d&&Array.isArray(d.portfolios)){
    ingestExtractedPositions(d,{allowClear:false});
    const metricsOnly=Object.assign({},d,{portfolios:d.portfolios.map(p=>{const q=Object.assign({},p);delete q.positions;return q})});
    st.portfolios=mergePortfolioSets(metricsOnly,st.portfolios,false);
    renderPortfolios();
    renderSignals();
  }
}
async function loadMacro(){
  const d=await get('macro','/api/v1/macro',7000);
  if(d){st.macro=d;renderInsights()}
}
async function loadIntelligence(){
  const [scorecard,progress,library]=await Promise.all([
    get('intelligence-scorecard','/api/v1/intelligence-scorecard',9000),
    get('learning-progress','/api/v1/learning-progress',9000),
    get('library-summary','/api/v1/library-summary',9000)
  ]);
  st.intelligence=normalizeIntelligence(scorecard,progress,library);
  renderIntelligence();
}
function refreshLiveState(){
  loadPortfolios();
  loadBootstrap();
}
function start(){
  loadBootstrap();
  setTimeout(loadPortfolios,250);
  setTimeout(loadIntelligence,500);
  setTimeout(loadMacro,1000);
  setInterval(loadPortfolios,10000);
  setInterval(loadBootstrap,15000);
  setInterval(loadIntelligence,60000);
  setInterval(loadMacro,120000);
  window.addEventListener('pageshow',()=>setTimeout(refreshLiveState,50));
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')setTimeout(refreshLiveState,50)});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
</script>
</body>
</html>'''

def apply_v90_ui(html):
    print('{"event":"V90_CANONICAL_RICH_UI","status":"installed"}', flush=True)
    return _CANONICAL_HTML
