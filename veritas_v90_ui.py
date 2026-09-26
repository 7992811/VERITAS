"""Canonical VERITAS Markets v9.0 UI.

Single-owner standalone dashboard. No legacy DOM patching, no stacked loaders.
"""
UI_VERSION = "veritas-ui-v9.0-canonical"

_CANONICAL_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VERITAS Markets</title>
<style>
:root{--bg:#0c1116;--card:#121920;--card2:#0f151b;--line:#26313a;--text:#e8edf1;--muted:#87939d;--ok:#59d694;--bad:#ef6767;--warn:#d6b75e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 Inter,Arial,sans-serif}
.wrap{max-width:1500px;margin:auto;padding:14px}.header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.brand{display:flex;align-items:center;gap:12px;min-width:0}.brand img{height:45px;max-width:330px;object-fit:contain}.brand-fallback{font-weight:800;letter-spacing:.18em;font-size:18px}.brand-fallback small{display:block;letter-spacing:.08em;font-size:8px;color:var(--muted);margin-top:3px}
.status{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.pill{border:1px solid var(--line);border-radius:999px;padding:4px 8px;font-size:9px;color:var(--muted)}
.ok{color:var(--ok)!important}.bad{color:var(--bad)!important}.warn{color:var(--warn)!important}.grid{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;min-width:0}.title{font-size:9px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:7px}
.actions,.assets,.portfolios,.positions,.trades{display:grid;gap:4px}.row{display:grid;gap:6px;align-items:center;border-top:1px solid rgba(255,255,255,.045);padding:5px 0;min-width:0}.row:first-child{border-top:0}
.action{grid-template-columns:65px 60px 42px minmax(100px,1fr) 78px 78px}.asset{grid-template-columns:62px 55px minmax(140px,1fr) 80px}
.pf{grid-template-columns:90px 1fr 64px 64px 52px}.pos{grid-template-columns:120px 56px 1fr 90px}.trade{grid-template-columns:130px 65px 65px 65px 80px 1fr}
.row b{font-size:10px}.row span{font-size:8px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tfs{display:grid;grid-template-columns:repeat(6,1fr);gap:3px}.tf{font-size:7px;text-align:center;padding:3px 2px;border:1px solid var(--line);border-radius:5px;color:var(--muted)}.tf b{display:block;font-size:7px}
.section{margin-top:8px}.full{grid-column:1/-1}.two{grid-column:span 2}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}.kpi{border:1px solid var(--line);border-radius:8px;padding:6px}.kpi span{display:block;color:var(--muted);font-size:7px}.kpi b{display:block;margin-top:2px;font-size:12px}
.msg{color:var(--muted);font-size:9px;padding:6px 0}.scroll{max-height:430px;overflow:auto;padding-right:2px}
@media(max-width:1000px){.grid{grid-template-columns:1fr}.two,.full{grid-column:1}.action{grid-template-columns:58px 52px 38px 1fr}.action .sl,.action .tp{display:none}.trade{grid-template-columns:100px 55px 55px 1fr}.trade .gross,.trade .fees{display:none}.brand img{max-width:220px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="brand">
      <img id="brandImage" src="/assets/veritas-markets-header.webp?v=9001" alt="VERITAS Markets" onerror="this.style.display='none';document.getElementById('brandFallback').style.display='block'">
      <div id="brandFallback" class="brand-fallback" style="display:none">VERITAS MARKETS<small>ЦИФРОВОЙ ИНВЕСТИЦИОННЫЙ КОМИТЕТ</small></div>
    </div>
    <div class="status">
      <span class="pill" id="sys">SYSTEM —</span>
      <span class="pill" id="db">DB —</span>
      <span class="pill" id="cells">DATA —</span>
      <span class="pill" id="stamp">—</span>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="title">Что делать сейчас</div>
      <div id="actions" class="actions"><div class="msg">Загрузка сигналов…</div></div>
    </div>

    <div class="card two">
      <div class="title">Общий взгляд по активам · 5м / 1ч / 4ч / 1д / 3д / 7д</div>
      <div id="assets" class="assets"><div class="msg">Загрузка рынка…</div></div>
    </div>

    <div class="card full section">
      <div class="title">Портфели</div>
      <div class="kpis">
        <div class="kpi"><span>Портфелей</span><b id="pfCount">—</b></div>
        <div class="kpi"><span>Открытых позиций</span><b id="openCount">—</b></div>
        <div class="kpi"><span>Лучшая доходность</span><b id="bestRet">—</b></div>
        <div class="kpi"><span>Макс. просадка</span><b id="maxDD">—</b></div>
      </div>
      <div id="portfolios" class="portfolios" style="margin-top:6px"><div class="msg">Загрузка портфелей…</div></div>
    </div>

    <div class="card two section">
      <div class="title">Открытые позиции</div>
      <div id="positions" class="positions"><div class="msg">Загрузка позиций…</div></div>
    </div>

    <div class="card section">
      <div class="title">Состояние загрузки</div>
      <div id="loadState" class="msg">Инициализация…</div>
    </div>

    <div class="card full section">
      <div class="title">Последние сделки</div>
      <div id="trades" class="trades scroll"><div class="msg">Загрузка журнала…</div></div>
    </div>
  </div>
</div>

<script>
(function(){
  'use strict';
  const ASSETS=['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'];
  const TFS=['5m','1h','4h','1d','3d','7d'];
  const state={signals:null,portfolios:null,trades:null,health:null,busy:{}};
  const el=id=>document.getElementById(id);
  const esc=v=>String(v==null?'—':v).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const label=a=>a==='NQ'?'NDXf':a==='CNYRUBF'?'CNYRUBf':a;
  const dir=x=>String((x&&x.research_decision)||(x&&x.decision)||'NO_TRADE');
  const cls=d=>d==='LONG'?'ok':d==='SHORT'?'bad':'warn';
  const arrow=d=>d==='LONG'?'↑':d==='SHORT'?'↓':'→';
  const num=(v,d=2)=>{const n=Number(v);return Number.isFinite(n)?n.toLocaleString('ru-RU',{maximumFractionDigits:d}):'—'};
  const rub=v=>{const n=Number(v);return Number.isFinite(n)?n.toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽':'—'};
  const pct=v=>{const n=Number(v);return Number.isFinite(n)?n.toFixed(2)+'%':'—'};

  async function fetchJson(key,url,timeout){
    if(state.busy[key]) return null;
    state.busy[key]=true;
    const ctl=new AbortController(), tm=setTimeout(()=>ctl.abort(),timeout);
    try{
      const r=await fetch(url,{cache:'no-store',signal:ctl.signal});
      if(!r.ok) throw new Error('HTTP '+r.status);
      return await r.json();
    }finally{
      clearTimeout(tm); state.busy[key]=false;
    }
  }

  function renderHealth(){
    const h=state.health||{};
    el('sys').textContent=h.ok?'SYSTEM ONLINE':'SYSTEM STARTING';
    el('sys').className='pill '+(h.ok?'ok':'warn');
    el('db').textContent=h.bootstrap_ready?'DB READY':'DB INIT';
    el('db').className='pill '+(h.bootstrap_ready?'ok':'warn');
  }

  function renderSignals(){
    const d=state.signals||{}, rows=Array.isArray(d.signals)?d.signals:[];
    const map={}; rows.forEach(x=>{if(x&&x.asset&&x.horizon)map[x.asset+'|'+x.horizon]=x});
    el('cells').textContent='DATA '+rows.length+'/42';
    el('cells').className='pill '+(rows.length>=42?'ok':rows.length?'warn':'bad');
    el('stamp').textContent=d.at?new Date(d.at).toLocaleTimeString():'—';

    const rank=x=>{
      const D=dir(x); if(D!=='LONG'&&D!=='SHORT')return -999;
      return (x.plan_eligible===false?0:2)+4*Number(x.horizon_structure_score||0)+2*Number(x.confidence||0)+Math.min(Number(x.expected_to_stop_ratio||0),3)+0.2*Number(x.independent_evidence_families||0);
    };
    const best=rows.filter(x=>['LONG','SHORT'].includes(dir(x))).sort((a,b)=>rank(b)-rank(a)).slice(0,5);
    el('actions').innerHTML=best.length?best.map(x=>{
      const D=dir(x), rr=x.expected_to_stop_ratio==null?'—':Number(x.expected_to_stop_ratio).toFixed(2);
      const why=x.entry_quality||x.plan_reason||x.regime||'—';
      return '<div class="row action"><b>'+label(x.asset)+'</b><b class="'+cls(D)+'">'+arrow(D)+' '+D+'</b><span>'+esc(x.horizon)+'</span><span>'+esc(why)+' · R/R '+rr+'</span><span class="sl">SL '+num(x.stop_price,4)+'</span><span class="tp">TP '+num(x.target_price,4)+'</span></div>';
    }).join(''):'<div class="msg">Сильных направленных идей сейчас нет.</div>';

    el('assets').innerHTML=ASSETS.map(a=>{
      const xs=TFS.map(tf=>map[a+'|'+tf]).filter(Boolean);
      const dirs=xs.map(dir), ln=dirs.filter(x=>x==='LONG').length, sn=dirs.filter(x=>x==='SHORT').length;
      const D=ln>sn?'LONG':sn>ln?'SHORT':'WAIT';
      const price=(map[a+'|5m']||xs[0]||{}).price;
      const tfs=TFS.map(tf=>{
        const x=map[a+'|'+tf], D2=x?dir(x):'';
        return '<div class="tf '+(x?cls(D2):'')+'"><b>'+tf+'</b>'+(x?arrow(D2):'—')+'</div>';
      }).join('');
      return '<div class="row asset"><b>'+label(a)+'</b><b class="'+cls(D)+'">'+arrow(D)+' '+D+'</b><div class="tfs">'+tfs+'</div><span style="text-align:right">'+num(price,4)+'</span></div>';
    }).join('');
  }

  function renderPortfolios(){
    const d=state.portfolios||{}, ps=Array.isArray(d.portfolios)?d.portfolios:[];
    const positions=[];
    ps.forEach(p=>(p.positions||[]).forEach(z=>positions.push(Object.assign({portfolio:p.name},z))));
    el('pfCount').textContent=ps.length+'/4';
    el('openCount').textContent=positions.length;
    const rets=ps.map(p=>Number(p.total_return_pct!=null?p.total_return_pct:((p.latest||{}).total_return_pct))).filter(Number.isFinite);
    const dds=ps.map(p=>Number(p.drawdown_pct!=null?p.drawdown_pct:(((p.latest||{}).drawdown!=null)?100*Number((p.latest||{}).drawdown):NaN))).filter(Number.isFinite);
    el('bestRet').textContent=rets.length?Math.max.apply(null,rets).toFixed(2)+'%':'—';
    el('maxDD').textContent=dds.length?Math.max.apply(null,dds).toFixed(2)+'%':'—';

    el('portfolios').innerHTML=ps.length?ps.map(p=>{
      const l=p.latest||p;
      const nav=l.nav_rub!=null?l.nav_rub:p.nav_rub;
      const ret=p.total_return_pct!=null?p.total_return_pct:l.total_return_pct;
      const dd=p.drawdown_pct!=null?p.drawdown_pct:(l.drawdown!=null?100*Number(l.drawdown):null);
      const gross=l.gross_leverage!=null?l.gross_leverage:p.gross_leverage;
      const wr=p.win_rate==null?'—':(100*Number(p.win_rate)).toFixed(1)+'%';
      return '<div class="row pf"><b>'+esc(p.name)+'</b><span>NAV '+rub(nav)+'</span><b class="'+(Number(ret||0)>=0?'ok':'bad')+'">'+pct(ret)+'</b><span>DD '+pct(dd)+'</span><span>'+wr+'</span></div>';
    }).join(''):'<div class="msg warn">Портфели пока не получены.</div>';

    el('positions').innerHTML=positions.length?positions.map(z=>{
      return '<div class="row pos"><b>'+esc(z.portfolio)+' · '+label(z.asset)+'</b><b class="'+(z.direction==='LONG'?'ok':'bad')+'">'+esc(z.direction)+'</b><span>вход '+num(z.avg_entry_price,4)+' · текущая '+num(z.last_price,4)+' · объём '+rub(z.notional_rub)+'</span><b class="'+(Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad')+'">'+rub(z.unrealized_pnl_rub)+'</b></div>';
    }).join(''):'<div class="msg">Открытых позиций нет.</div>';
  }

  function renderTrades(){
    const d=state.trades||{}, rows=Array.isArray(d.trades)?d.trades:[];
    el('trades').innerHTML=rows.length?rows.slice(0,80).map(t=>{
      const net=t.net_pnl_rub;
      return '<div class="row trade"><b>'+esc(t.portfolio_name)+' · '+label(t.asset)+'</b><span>'+esc(t.direction)+'</span><span>'+esc(t.horizon||'—')+'</span><span>'+esc(t.status||'—')+'</span><b class="'+(Number(net||0)>=0?'ok':'bad')+'">'+rub(net)+'</b><span>'+ (t.closed_at?new Date(t.closed_at).toLocaleString():'открыта '+(t.opened_at?new Date(t.opened_at).toLocaleString():'—')) +'</span></div>';
    }).join(''):'<div class="msg">Сделки пока не получены.</div>';
  }

  function loadStateText(){
    const parts=[];
    parts.push(state.signals?'сигналы ✓':'сигналы …');
    parts.push(state.portfolios?'портфели ✓':'портфели …');
    parts.push(state.trades?'сделки ✓':'сделки …');
    el('loadState').textContent=parts.join(' · ');
  }

  async function loadHealth(){
    try{const d=await fetchJson('health','/healthz',3000);if(d){state.health=d;renderHealth()}}catch(e){}
  }
  async function loadSignals(){
    try{const d=await fetchJson('signals','/api/v1/signals',6000);if(d){state.signals=d;renderSignals()}}catch(e){}
    loadStateText();
  }
  async function loadPortfolios(){
    try{const d=await fetchJson('portfolios','/api/v1/paper-portfolios',6000);if(d){state.portfolios=d;renderPortfolios()}}catch(e){}
    loadStateText();
  }
  async function loadTrades(){
    try{const d=await fetchJson('trades','/api/v1/portfolio-trades',8000);if(d){state.trades=d;renderTrades()}}catch(e){}
    loadStateText();
  }

  function initial(){
    loadHealth(); loadSignals(); loadPortfolios(); loadTrades();
    setInterval(loadHealth,30000);
    setInterval(loadSignals,30000);
    setInterval(loadPortfolios,30000);
    setInterval(loadTrades,60000);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initial,{once:true});else initial();
})();
</script>
</body>
</html>'''

def apply_v90_ui(html):
    print('{"event":"V90_CANONICAL_STANDALONE_UI","status":"installed"}', flush=True)
    return _CANONICAL_HTML
