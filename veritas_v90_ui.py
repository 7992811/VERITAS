"""Canonical VERITAS Markets v9.0 UI.

Single-owner dashboard with full decision, portfolio, trade, learning and data-quality views.
No legacy DOM patching or duplicate network loaders.
"""
UI_VERSION = "veritas-ui-v9.0-canonical-rich"

_CANONICAL_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VERITAS Markets</title>
<style>
:root{--bg:#0b1015;--card:#111820;--card2:#0e151c;--line:#26323c;--text:#e8edf2;--muted:#8b97a2;--ok:#59d694;--bad:#ef6767;--warn:#d6b75e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 Inter,Arial,sans-serif}
.wrap{max-width:1560px;margin:auto;padding:14px}.brand-hero{width:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:10px 0 8px;margin-bottom:6px;border-bottom:1px solid rgba(255,255,255,.05)}
.brand-veritas{font-size:clamp(30px,4.2vw,58px);line-height:.95;font-weight:800;letter-spacing:.22em;color:#e7edf2;text-align:center;padding-left:.22em}
.brand-markets{font-size:clamp(11px,1.35vw,18px);font-weight:700;letter-spacing:.58em;color:#9eabb5;text-align:center;padding-left:.58em;margin-top:7px}
.brand-tagline{font-size:9px;letter-spacing:.16em;color:#65727d;text-transform:uppercase;margin-top:7px;text-align:center}
.status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin:0 0 10px}
.pill{border:1px solid var(--line);border-radius:9px;padding:7px 9px;font-size:10px;color:var(--muted);text-align:center;background:rgba(255,255,255,.015)}
.ok{color:var(--ok)!important}.bad{color:var(--bad)!important}.warn{color:var(--warn)!important}
.grid{display:grid;grid-template-columns:1.1fr 1.1fr .9fr;gap:8px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;min-width:0}
.title{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:7px}
.full{grid-column:1/-1}.two{grid-column:span 2}.section{margin-top:8px}.msg{font-size:10px;color:var(--muted);padding:5px 0}
.row{display:grid;gap:6px;align-items:center;border-top:1px solid rgba(255,255,255,.045);padding:5px 0;min-width:0}.row:first-child{border-top:0}
.row b{font-size:11px}.row span{font-size:9px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.action{grid-template-columns:74px 92px 46px minmax(130px,1fr) 82px 82px}.asset{grid-template-columns:170px 78px minmax(0,1fr);gap:8px}.asset-main{min-width:0;display:flex;align-items:baseline;gap:9px}.asset-main b{display:inline-block}.asset-price{display:inline-block!important;font-size:10px!important;color:#aab6c1!important;font-variant-numeric:tabular-nums}.asset-bias{text-align:left;white-space:nowrap}.asset-tfline{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:9px!important;color:#9aa6b1!important}
.tfs{display:grid;grid-template-columns:repeat(6,minmax(33px,1fr));gap:3px}.tf{font-size:7px;text-align:center;padding:3px 2px;border:1px solid var(--line);border-radius:5px;color:var(--muted)}
.matrix-wrap{overflow-x:hidden;overflow-y:visible}.matrix{width:100%;table-layout:fixed;border-collapse:separate;border-spacing:6px 4px}.matrix th{font-size:11.5px;color:#aeb9c4;font-weight:650;padding:3px 3px;line-height:1.05}
.matrix th:first-child{width:126px}.matrix th.asset-head{text-align:left;width:126px;min-width:0;padding-left:0}.matrix td{padding:0;min-width:0;width:auto}
.asset-label{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:700;color:#dfe6ed;white-space:nowrap;transform:translateX(-3px)}
.asset-icon{width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.14);background:#18212a;color:#eaf0f5;font-size:11px;font-weight:800;box-shadow:inset 0 0 0 1px rgba(255,255,255,.025);flex:0 0 24px}
.asset-icon.btc{color:#ffb35a;border-color:rgba(255,179,90,.35)}.asset-icon.eth{color:#bda7ff;border-color:rgba(189,167,255,.32)}
.asset-icon.ndxf{color:#79b8ff;border-color:rgba(121,184,255,.32)}.asset-icon.brent{color:#c3d0dc;border-color:rgba(195,208,220,.28)}
.asset-icon.gold{color:#e8c45a;border-color:rgba(232,196,90,.35)}.asset-icon.moex{color:#8bb9ff;border-color:rgba(139,185,255,.3)}
.asset-icon.cny{color:#efc25a;border-color:rgba(239,194,90,.34);font-size:9px}
.cell{width:100%;min-height:50px;border:0;border-radius:0;background:transparent;color:var(--text);padding:4px 2px;cursor:pointer;font-size:11px;display:flex;flex-direction:column;align-items:center;justify-content:center}
.cell:hover{background:rgba(255,255,255,.025);border-radius:7px}.cell.sel{outline:1px solid rgba(145,164,180,.45);outline-offset:0;border-radius:7px}
.cell small{display:block;font-size:9.5px;color:#b2bdc7;margin-top:4px;line-height:1;font-weight:600;white-space:nowrap}
.sig-dot{display:inline-block;width:19px;height:19px;border-radius:50%;vertical-align:middle;position:relative}
.sig-dot.long{background:var(--ok);box-shadow:0 0 11px rgba(89,214,148,.42)}.sig-dot.short{background:var(--bad);box-shadow:0 0 11px rgba(239,103,103,.42)}.sig-dot.wait{background:var(--warn);box-shadow:0 0 9px rgba(214,183,94,.32)}
.sig-dot.super{width:24px;height:24px;background:transparent!important;border:3px solid currentColor;box-shadow:none}
.sig-dot.super::after{content:'';position:absolute;left:50%;top:50%;width:9px;height:9px;border-radius:50%;transform:translate(-50%,-50%)}
.sig-dot.super.long{color:var(--ok);border-color:var(--ok);box-shadow:0 0 9px rgba(89,214,148,.75),0 0 19px rgba(89,214,148,.30)}
.sig-dot.super.long::after{background:var(--ok);box-shadow:0 0 7px rgba(89,214,148,.9)}
.sig-dot.super.short{color:var(--bad);border-color:var(--bad);box-shadow:0 0 9px rgba(239,103,103,.75),0 0 19px rgba(239,103,103,.30)}
.sig-dot.super.short::after{background:var(--bad);box-shadow:0 0 7px rgba(239,103,103,.9)}
.super-label{font-weight:800;letter-spacing:.04em;color:#eef3f7!important}
.detail-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}.metric{border:1px solid var(--line);border-radius:8px;padding:6px;min-width:0}
.metric span{display:block;font-size:8px;color:var(--muted)}.metric b{display:block;font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.detail-note{margin-top:6px;border-top:1px solid var(--line);padding-top:6px;font-size:9px;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}.kpi{border:1px solid var(--line);border-radius:8px;padding:6px}.kpi span{display:block;color:var(--muted);font-size:8px}.kpi b{display:block;margin-top:2px;font-size:12px}
.portfolio-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:7px}.portfolio-card{border:1px solid var(--line);border-radius:10px;padding:9px;background:var(--card2);min-width:0}.portfolio-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}.portfolio-head b{font-size:12px}.portfolio-nav{font-size:11px;color:#dce4ea;font-weight:650}.portfolio-sub{font-size:9px;color:var(--muted);margin-top:2px}.portfolio-metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:5px}.portfolio-metric{border-top:1px solid rgba(255,255,255,.045);padding-top:5px}.portfolio-metric span{display:block;font-size:8px;color:var(--muted)}.portfolio-metric b{display:block;font-size:10px;margin-top:1px}.pos{grid-template-columns:145px 62px minmax(180px,1fr) 110px}
.trade-card{border-top:1px solid rgba(255,255,255,.05);padding:8px 0}.trade-card:first-child{border-top:0}.trade-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.trade-head b{font-size:11px}.trade-result{font-size:11px;font-weight:700}.trade-meta{font-size:9px;color:var(--muted);margin-top:3px}.trade-money{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:5px}.trade-money span{font-size:8px;color:var(--muted)}.trade-money b{display:block;font-size:9px;color:var(--text);margin-top:1px}
.insight-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.insight{border:1px solid var(--line);border-radius:8px;padding:7px;min-width:0}
.insight h4{margin:0 0 5px;font-size:9px;color:var(--muted);font-weight:500;text-transform:uppercase}.insight div{font-size:9px;line-height:1.6}
.scroll{max-height:450px;overflow:auto;padding-right:2px}
@media(max-width:1050px){.grid{grid-template-columns:1fr}.two,.full{grid-column:1}.action{grid-template-columns:70px 86px 40px 1fr}.action .sl,.action .tp{display:none}.detail-grid,.kpis,.insight-grid{grid-template-columns:repeat(2,1fr)}.portfolio-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.status{grid-template-columns:repeat(2,minmax(0,1fr))}.trade-money{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand-hero">
    <div class="brand-veritas">VERITAS</div>
    <div class="brand-markets">MARKETS</div>
    <div class="brand-tagline">Цифровой инвестиционный комитет</div>
  </div>
  <div class="status">
    <span class="pill" id="sys">СИСТЕМА · —</span>
    <span class="pill" id="db">БАЗА · —</span>
    <span class="pill" id="cells">ДАННЫЕ · —</span>
    <span class="pill" id="stamp">ОБНОВЛЕНО · —</span>
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
      <div class="title" style="font-size:12px;font-weight:700;letter-spacing:.06em;margin-bottom:5px">Матрица сигналов · 7 активов × 6 таймфреймов</div>
      <div class="matrix-wrap"><table class="matrix"><thead><tr><th>Актив</th><th>5м</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th></tr></thead><tbody id="matrixBody"></tbody></table></div>
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
        <div class="kpi"><span>Макс. просадка</span><b id="maxDD">—</b></div>
      </div>
      <div id="portfolios" style="margin-top:6px"><div class="msg">Загрузка портфелей…</div></div>
    </div>

    <div class="card full section">
      <div class="title">Открытые позиции</div>
      <div id="positions"><div class="msg">Загрузка позиций…</div></div>
    </div>

    <div class="card full section">
      <div class="title">Последние сделки</div>
      <div id="trades" class="scroll"><div class="msg">Загрузка журнала…</div></div>
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
const AS=['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'], TF=['5m','1h','4h','1d','3d','7d'];
const st={signals:null,portfolios:null,trades:null,health:null,learning:null,quality:null,horizon:null,macro:null,busy:{},selected:null};
const $=id=>document.getElementById(id);
const esc=v=>String(v==null?'—':v).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const lab=a=>a==='NQ'?'NDXf':a==='CNYRUBF'?'CNYRUBf':a;
const assetIcon=a=>({BTC:'₿',ETH:'Ξ',NQ:'N',BRENT:'◆',GOLD:'Au',MOEX:'M',CNYRUBF:'¥₽'}[a]||'•');
const assetIconClass=a=>({BTC:'btc',ETH:'eth',NQ:'ndxf',BRENT:'brent',GOLD:'gold',MOEX:'moex',CNYRUBF:'cny'}[a]||'');
const dir=x=>String((x&&x.research_decision)||(x&&x.decision)||'NO_TRADE');
const tier=x=>String((x&&x.signal_tier)||dir(x));
const tierLabel=x=>tier(x)==='SUPER_LONG'?'SUPER LONG':tier(x)==='SUPER_SHORT'?'SUPER SHORT':dir(x)==='LONG'?'LONG':dir(x)==='SHORT'?'SHORT':'WAIT';
const cls=d=>d==='LONG'?'ok':d==='SHORT'?'bad':'warn', ar=d=>d==='LONG'?'↑':d==='SHORT'?'↓':'→';
const n=(v,d=2)=>{v=Number(v);return Number.isFinite(v)?v.toLocaleString('ru-RU',{maximumFractionDigits:d}):'—'};
const rub=v=>{v=Number(v);return Number.isFinite(v)?v.toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽':'—'};
const pct=v=>{v=Number(v);return Number.isFinite(v)?v.toFixed(2)+'%':'—'};
const bool=v=>v===true?'ДА':v===false?'НЕТ':'—';
const planOf=x=>(x&&x.trade_plan)||{};
const rrOf=x=>x?.expected_to_stop_ratio??planOf(x).expected_to_stop_ratio;
const stopOf=x=>x?.stop_price??planOf(x).stop_price;
const targetOf=x=>x?.target_price??planOf(x).target_price??planOf(x).take_price;
const dirRu=d=>d==='LONG'?'Лонг':d==='SHORT'?'Шорт':'Нет позиции';
const tfRu=tf=>({'5m':'5 мин','1h':'1 ч','4h':'4 ч','1d':'1 день','3d':'3 дня','7d':'7 дней'}[tf]||tf||'—');
const holdRu=s=>{s=Number(s);if(!Number.isFinite(s)||s<0)return'—';const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.max(0,Math.floor((s%3600)/60));return(d?d+' д ':'')+(h?h+' ч ':'')+(m+' мин')};
const dateRu=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const exitRu=r=>{r=String(r||'').toUpperCase();const m={TAKE_PROFIT:'Цель достигнута',TP:'Цель достигнута',STOP:'Стоп',STOP_LOSS:'Стоп',SIGNAL_FLIP:'Смена сигнала',TIMEOUT:'Выход по времени',PROFIT_HARVEST:'Фиксация прибыли',MANUAL:'Ручное закрытие',CLOSED:'Закрыта'};return m[r]||'Закрытие системой'};

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
  $('cells').textContent='ДАННЫЕ · '+rows.length+'/42';$('cells').className='pill '+(rows.length>=42?'ok':rows.length?'warn':'bad');
  $('stamp').textContent='ОБНОВЛЕНО · '+(d.at?new Date(d.at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—');

  const rank=x=>{const D=dir(x),T=tier(x);if(!['LONG','SHORT'].includes(D))return-999;const superBoost=(T==='SUPER_LONG'||T==='SUPER_SHORT')?5:0;return superBoost+(x.plan_eligible===false?0:2)+4*Number(x.horizon_structure_score||0)+2*Number(x.confidence||0)+Math.min(Number(x.expected_to_stop_ratio||0),3)+.2*Number(x.independent_evidence_families||0)};
  const best=rows.filter(x=>{const D=dir(x),rr=Number(rrOf(x));return ['LONG','SHORT'].includes(D)&&String(x.entry_quality||'')!=='INVALIDATED'&&String(x.decision_stage||'')!=='INVALIDATED'&&Number.isFinite(rr)&&rr>0}).sort((a,b)=>rank(b)-rank(a)).slice(0,5);
  $('actions').innerHTML=best.length?best.map(x=>{const rr=Number(rrOf(x)),ready=x.execution_eligible===true||planOf(x).eligible===true;const state=ready?'ГОТОВ К ВХОДУ':'НАБЛЮДЕНИЕ';const sig=tier(x)==='SUPER_LONG'?'СУПЕР ЛОНГ':tier(x)==='SUPER_SHORT'?'СУПЕР ШОРТ':dirRu(dir(x));return'<div class="row action"><b>'+lab(x.asset)+'</b><b class="'+cls(dir(x))+' '+((tier(x)==='SUPER_LONG'||tier(x)==='SUPER_SHORT')?'super-label':'')+'">'+ar(dir(x))+' '+sig+'</b><span>'+tfRu(x.horizon)+'</span><span>'+state+' · R/R '+rr.toFixed(2)+' · '+esc(x.regime||'режим не определён')+'</span><span class="sl">Стоп '+n(stopOf(x),4)+'</span><span class="tp">Цель '+n(targetOf(x),4)+'</span></div>'}).join(''):'<div class="msg">Готовых направленных входов сейчас нет — система ждёт подтверждения структуры и достаточного R/R.</div>';

  $('assets').innerHTML=AS.map(a=>{const xs=TF.map(tf=>map[a+'|'+tf]).filter(Boolean),ds=xs.map(dir),ln=ds.filter(x=>x==='LONG').length,sn=ds.filter(x=>x==='SHORT').length,D=ln>sn?'LONG':sn>ln?'SHORT':'WAIT',p=(map[a+'|5m']||xs[0]||{}).price;return'<div class="row asset"><div class="asset-main"><b>'+lab(a)+'</b><span class="asset-price">'+n(p,4)+'</span></div><b class="asset-bias '+cls(D)+'">'+ar(D)+' '+(D==='LONG'?'ЛОНГ':D==='SHORT'?'ШОРТ':'ЖДАТЬ')+'</b><span class="asset-tfline">'+TF.map(tf=>{const x=map[a+'|'+tf];return tfRu(tf)+' '+(x?ar(dir(x)):'—')}).join('   ')+'</span></div>'}).join('');

  $('matrixBody').innerHTML=AS.map(a=>'<tr><th class="asset-head"><div class="asset-label"><span class="asset-icon '+assetIconClass(a)+'">'+assetIcon(a)+'</span><span>'+lab(a)+'</span></div></th>'+TF.map(tf=>{const x=map[a+'|'+tf];if(!x)return'<td><button class="cell"><span class="sig-dot wait" style="opacity:.25"></span><small>—</small></button></td>';const D=dir(x),T=tier(x),conf=100*Number(x.confidence||0),isSuper=(T==='SUPER_LONG'||T==='SUPER_SHORT'),dc=D==='LONG'?'long':D==='SHORT'?'short':'wait',tag=T==='SUPER_LONG'?'SL':T==='SUPER_SHORT'?'SS':D==='LONG'?'L':D==='SHORT'?'S':'—';return'<td><button class="cell" data-k="'+a+'|'+tf+'" title="'+esc(tierLabel(x))+' · '+conf.toFixed(0)+'%"><span class="sig-dot '+dc+(isSuper?' super':'')+'"></span><small class="'+(isSuper?'super-label':'')+'">'+tag+' · '+conf.toFixed(0)+'%</small></button></td>'}).join('')+'</tr>').join('');
  document.querySelectorAll('.cell[data-k]').forEach(b=>b.onclick=()=>selectSignal(b.dataset.k));
  if(!st.selected&&rows.length){const x=best[0]||rows[0];st.selected=x.asset+'|'+x.horizon}
  if(st.selected)selectSignal(st.selected,false);
}

function selectSignal(k,scroll=false){
  st.selected=k;document.querySelectorAll('.cell').forEach(x=>x.classList.toggle('sel',x.dataset.k===k));
  const x=signalMap()[k];if(!x){$('detail').innerHTML='<div class="msg">Нет данных.</div>';return}
  const sl=x.structural_levels||{}, plan=x.trade_plan||{}, D=dir(x);
  const items=[
    ['Актив',lab(x.asset)],['ТФ',x.horizon],['Сигнал',tierLabel(x)],['Направление',D],['Цена',n(x.price,4)],
    ['Confidence',x.confidence==null?'—':(100*Number(x.confidence)).toFixed(1)+'%'],['Structure',n(x.horizon_structure_score,3)],['Regime',x.regime],['Stage',x.decision_stage],
    ['Entry quality',x.entry_quality],['Подтверждения',x.independent_evidence_families],['Execution',bool(x.execution_eligible)],['Source gate',bool(x.source_gate_pass)],
    ['SL',n(x.stop_price??plan.stop_price,4)],['TP',n(x.target_price??plan.target_price,4)],['R/R',n(x.expected_to_stop_ratio??plan.expected_to_stop_ratio,2)],['Expected move',x.expected_move_pct==null?'—':(100*Number(x.expected_move_pct)).toFixed(2)+'%'],
    ['Support',n(sl.support,4)],['Resistance',n(sl.resistance,4)],['SMA18',n(sl.sma18,4)],['SMA50',n(sl.sma50,4)]
  ];
  $('detail').innerHTML='<div class="detail-grid">'+items.map(([a,b])=>'<div class="metric"><span>'+a+'</span><b class="'+(a==='Направление'?cls(D):'')+'">'+esc(b)+'</b></div>').join('')+'</div><div class="detail-note">Причина: <b>'+esc(x.plan_reason||plan.reason||'—')+'</b><br>Execution reason: '+esc(x.execution_reason||'—')+' · Market open: '+bool(x.market_open)+' · stale: '+bool(x.snapshot_stale)+'</div>';
  if(scroll)$('detail').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function renderPortfolios(){
  const d=st.portfolios||{},ps=Array.isArray(d.portfolios)?d.portfolios:[],positions=[];
  ps.forEach(p=>(p.positions||[]).forEach(z=>positions.push(Object.assign({portfolio:p.name},z))));
  $('pfCount').textContent=ps.length+'/4';$('openCount').textContent=positions.length;
  const rets=ps.map(p=>Number(p.total_return_pct!=null?p.total_return_pct:((p.latest||{}).total_return_pct))).filter(Number.isFinite);
  const dds=ps.map(p=>Number(p.drawdown_pct!=null?p.drawdown_pct:(((p.latest||{}).drawdown!=null)?100*Number((p.latest||{}).drawdown):NaN))).filter(Number.isFinite);
  $('bestRet').textContent=rets.length?Math.max(...rets).toFixed(2)+'%':'—';$('maxDD').textContent=dds.length?Math.max(...dds).toFixed(2)+'%':'—';
  $('portfolios').className='portfolio-grid';
  $('portfolios').innerHTML=ps.length?ps.map(p=>{const l=p.latest||p,nav=l.nav_rub??p.nav_rub,usd=l.nav_usd??p.nav_usd,ret=p.total_return_pct??l.total_return_pct,dd=p.drawdown_pct??(l.drawdown!=null?100*Number(l.drawdown):null),gross=l.gross_leverage??p.gross_leverage,net=l.net_exposure??p.net_exposure,cash=l.cash_equivalent_fraction??p.cash_equivalent_fraction,wr=p.win_rate==null?null:100*Number(p.win_rate),closed=Number(p.closed_trades||0),wins=Number(p.wins||0),pnl=Number(p.closed_trade_pnl_rub||0),openN=(p.positions||[]).length;return'<div class="portfolio-card"><div class="portfolio-head"><div><b>'+esc(p.name)+'</b><div class="portfolio-sub">'+openN+' открытых · '+closed+' закрытых сделок</div></div><b class="'+(Number(ret||0)>=0?'ok':'bad')+'">'+pct(ret)+'</b></div><div class="portfolio-nav">'+rub(nav)+' <span class="portfolio-sub">· 
  $('positions').innerHTML=positions.length?positions.map(z=>'<div class="row pos"><b>'+esc(z.portfolio)+' · '+lab(z.asset)+'</b><b class="'+(z.direction==='LONG'?'ok':'bad')+'">'+esc(z.direction)+'</b><span>'+esc(z.horizon||'—')+' · вход '+n(z.avg_entry_price,4)+' · текущая '+n(z.last_price,4)+' · SL '+n(z.stop_price,4)+' · TP '+n(z.take_price??z.target_price,4)+' · объём '+rub(z.notional_rub)+'</span><b class="'+(Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad')+'">'+rub(z.unrealized_pnl_rub)+'</b></div>').join(''):'<div class="msg">Открытых позиций нет.</div>';
}

function renderTrades(){
  const rows=Array.isArray(st.trades&&st.trades.trades)?st.trades.trades:[];
  $('trades').innerHTML=rows.length?rows.slice(0,80).map(t=>{const net=Number(t.net_pnl_rub||0),g=Number(t.gross_pnl_rub||0),fees=Number(t.fees_rub||0),fund=Number(t.funding_rub||0),p=t.payload||{},entry=Number(t.avg_entry_price||0),exit=Number(t.avg_exit_price||0),D=String(t.direction||''),heldRaw=t.holding_duration_seconds??t.held_seconds,held=heldRaw!=null?Number(heldRaw):(t.opened_at&&t.closed_at?Math.max(0,(new Date(t.closed_at)-new Date(t.opened_at))/1000):null),priceRet=(entry>0&&exit>0)?100*(D==='SHORT'?(entry/exit-1):(exit/entry-1)):null,retPct=p.return_pct??t.return_pct??priceRet,reason=exitRu(t.exit_reason||p.exit_reason||t.status);return'<div class="trade-card"><div class="trade-head"><b>'+esc(t.portfolio_name)+' · '+lab(t.asset)+' · '+dirRu(D)+'</b><div class="trade-result '+(net>=0?'ok':'bad')+'">'+rub(net)+(retPct==null?'':' · '+(Number(retPct)>=0?'+':'')+Number(retPct).toFixed(2)+'%')+'</div></div><div class="trade-meta">'+tfRu(t.horizon)+' · Вход '+n(entry,4)+' → выход '+n(exit,4)+' · Закрыта '+dateRu(t.closed_at)+' · Удержание '+holdRu(held)+' · Причина: '+reason+'</div><div class="trade-money"><span>Доход от цены<b>'+rub(g)+'</b></span><span>Комиссия<b>'+rub(fees)+'</b></span><span>Фондирование<b>'+rub(fund)+'</b></span><span>Итоговый результат<b class="'+(net>=0?'ok':'bad')+'">'+rub(net)+'</b></span></div></div>'}).join(''):'<div class="msg">Закрытых сделок пока нет.</div>';
}

function renderInsights(){
  const l=st.learning||{}, q=st.quality||{}, h=st.horizon||{}, m=st.macro||{};
  const wr=l.win_rate==null?'—':(100*Number(l.win_rate)).toFixed(1)+'%';
  $('learning').innerHTML='<b>Закрытых сделок:</b> '+esc(l.closed_trades??'—')+'<br><b>Прибыльных:</b> '+esc(l.wins??'—')+'<br><b>Win-rate:</b> '+wr+'<br><b>Память опыта:</b> '+esc(l.experience_storage||'—');
  $('quality').innerHTML='<b>Матрица:</b> '+esc(q.cells??'—')+'/'+esc(q.expected_cells??42)+'<br><b>Источник подтверждён:</b> '+esc(q.source_verified_cells??'—')+' ячеек<br><b>Можно исполнять:</b> '+esc(q.execution_eligible_cells??'—')+' ячеек<br><b>Устаревших:</b> '+esc(q.stale_cells??'—');
  $('horizon').innerHTML=TF.map(tf=>'<b>'+tf+':</b> '+esc(h[tf]??0)+'/7 активов').join('<br>');
  const macroObj=m.macro||m, regime=m.regime||{};
  const macroLines=[];
  if(regime&&typeof regime==='object'){if(regime.regime)macroLines.push('<b>Режим:</b> '+esc(regime.regime));if(regime.summary)macroLines.push(esc(regime.summary))}
  ['dxy','vix','us10y','oil','gold'].forEach(k=>{if(macroObj&&macroObj[k]!=null)macroLines.push('<b>'+k.toUpperCase()+':</b> '+esc(macroObj[k]))});
  $('macro').innerHTML=macroLines.slice(0,6).join('<br>')||'Макро-контекст обновляется отдельно и не блокирует торговые данные.';
}

function applyBootstrap(d){
  if(!d)return;
  st.health={ok:true,bootstrap_ready:!!(d.health&&d.health.bootstrap_ready)};
  st.signals={signals:d.signals||[],at:d.at,status:d.status};
  st.portfolios={portfolios:d.portfolios||[]};
  st.trades={trades:d.trades||[]};
  st.learning=d.learning_summary||{};
  st.quality=d.data_quality_summary||{};
  st.horizon=d.horizon_summary||{};
  renderHealth();renderSignals();renderPortfolios();renderTrades();renderInsights();
}

async function loadBootstrap(){
  const d=await get('bootstrap','/api/v1/dashboard-bootstrap',8000);
  if(d)applyBootstrap(d);
}
async function loadMacro(){
  const d=await get('macro','/api/v1/macro',7000);
  if(d){st.macro=d;renderInsights()}
}
function start(){loadBootstrap();setTimeout(loadMacro,1000);setInterval(loadBootstrap,30000);setInterval(loadMacro,120000)}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
</script>
</body>
</html>'''

def apply_v90_ui(html):
    print('{"event":"V90_CANONICAL_RICH_UI","status":"installed"}', flush=True)
    return _CANONICAL_HTML
