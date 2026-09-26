"""VERITAS Markets 9.0 UI layer.

Ports the last approved 86.x interface/branding onto the 9.0 runtime.
This module changes presentation only; trading logic and persistence remain in v9.0.
"""
import json
import re

UI_VERSION = 'veritas-ui-v9.0'

def apply_v90_ui(html):
    value = str(html)
    value = value.replace('Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.',
                          'Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Champion, Challenger, Impulse и Aggressive. Реальные деньги не используются.')
    value = value.replace('Открытых позиций нет — оба портфеля в cash.','Открытых позиций нет — портфели в cash.')
    value = value.replace('30 ячеек · ~','42 ячейки · ~').replace('35 ячеек · ~','42 ячейки · ~').replace('6 активов × 5 ТФ','7 активов × 6 ТФ').replace('7 активов × 5 ТФ','7 активов × 6 ТФ').replace('6/6 активов','7/7 активов')
    # V90_FULL_5M_MATRIX
    value = value.replace(
        '<th>Актив</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th>',
        '<th>Актив</th><th>5м</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th>'
    )
    value = value.replace(
        "const tfOrder=['1h','4h','1d','3d','7d']",
        "const tfOrder=['5m','1h','4h','1d','3d','7d']"
    )
    value = value.replace(
        ".superstrip{display:grid;grid-template-columns:repeat(5,minmax(105px,1fr));",
        ".superstrip{display:grid;grid-template-columns:repeat(6,minmax(90px,1fr));"
    )
    value = value.replace(
        ".matrix-wrap{overflow-x:hidden;",
        ".matrix-wrap{overflow-x:auto;"
    )
    value = value.replace(
        ".signal-table{width:min(100%,460px);",
        ".signal-table{width:min(100%,540px);"
    )
    value = value.replace(
        "ожидается ${hi.expected_signal_cells??30} ячеек (6 активов × 5 ТФ)",
        "ожидается ${hi.expected_signal_cells??42} ячейки (7 активов × 6 ТФ)"
    )
    value = value.replace('NDX','NQ')
    # V86.2 BRAND_AND_HEADER_REFINEMENT
    _brand_markup = '''<div class="veritas-brandlock veritas-brand-artwork">
      <img class="veritas-brand-image" src="/assets/veritas-markets-header.webp?v=90" alt="VERITAS Markets — Цифровой инвестиционный комитет">
    </div>'''
    value = re.sub(
        r'<h1>VERITAS Markets</h1>\s*<div class="sub">[^<]*</div>',
        _brand_markup,
        value,count=1,flags=re.I
    )
    value = value.replace(
        "document.getElementById('users').textContent=`${um.unique_users??0} / ${um.online_users??0}`;document.getElementById('userssmall').textContent='уникальных / онлайн сейчас';",
        "document.getElementById('users').textContent=`${um.online_users??0} / ${um.unique_users??0}`;document.getElementById('userssmall').textContent='онлайн сейчас / уникальных';"
    )
    # V86.2 DEEP_TAB_TRUTHFUL_NULLS
    value = value.replace(
        "рынок каждые ${Math.round((au.market_learning_cycle_seconds||0)/60)} мин · знания каждые ${Math.round((au.knowledge_discovery_interval_seconds||0)/3600)} ч<br>Postgres: ${au.persistent_experience_storage?'durable':'нет'} · uptime ${Math.round((au.process_uptime_seconds||0)/60)} мин",
        "рынок каждые ${au.market_learning_cycle_seconds==null?'—':Math.round(au.market_learning_cycle_seconds/60)} мин · знания каждые ${au.knowledge_discovery_interval_seconds==null?'—':Math.round(au.knowledge_discovery_interval_seconds/3600)} ч<br>Postgres: ${au.persistent_experience_storage?'durable':'нет'} · uptime ${au.process_uptime_seconds==null?'—':Math.round(au.process_uptime_seconds/60)} мин"
    )
    value = value.replace(
        "Источники <b>${lrn.sources_total??'—'}</b> · +${lrn.sources_added_today??0} сегодня<br>Правила <b>${lrn.rules_total??'—'}</b> · +${lrn.rules_added_today??0} сегодня<br>Авто-правила сегодня ${lrn.auto_rules_imported_today??0} · кандидаты +${lrn.candidates_added_today??0}",
        "Источники <b>${lrn.sources_total??'—'}</b> · +${lrn.sources_added_today??'—'} сегодня<br>Правила <b>${lrn.rules_total??'—'}</b> · +${lrn.rules_added_today??'—'} сегодня<br>Авто-правила сегодня ${lrn.auto_rules_imported_today??'—'} · кандидаты +${lrn.candidates_added_today??'—'}"
    )
    value = re.sub(r'<div class="k">(?:RUONIA|Руониа)</div><div[^>]*>[^<]*</div>','',value,flags=re.I)
    # Remove the complete legacy RUONIA/USD-RUB portfolio footer.
    # Do not truncate the JS expression: truncation caused the visible "oFixed(2)+'%'}" artifact.
    value = re.sub(
        r"<br>RUONIA \\$\\{x\\.ruonia==null\\?'—':Number\\(x\\.ruonia\\)\\.toFixed\\(2\\)\\+'%'\\} · USD/RUB \\$\\{x\\.usdrub==null\\?'—':Number\\(x\\.usdrub\\)\\.toFixed\\(4\\)\\}",
        '',
        value,count=1,flags=re.I
    )
    value = value.replace('Последние сделки','Закрытые сделки · сегодня / архив').replace('ПОСЛЕДНИЕ СДЕЛКИ','ЗАКРЫТЫЕ СДЕЛКИ · СЕГОДНЯ / АРХИВ').replace('Закрытые сделки · CLOSED_FINAL','Закрытые сделки · сегодня / архив')
    value = value.replace("${p.name==='Champion'?'70%+':'77%+'}","${p.badge||''}")
    value = value.replace('Шаг позиции 5% · gross ≤ 2,5× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.',
                          'Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · риск по стопу 1–2% NAV · hard stop DD 8–12% в зависимости от мандата.')
    
    replacement = """posel.innerHTML=positions.length?(()=>{const fmtPx=x=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:4});const fmtUsd=x=>x==null?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:0})+' USD';const fmtHold=x=>x==null?'—':Number(x)>=86400?(Number(x)/86400).toFixed(1)+' д':Number(x)>=3600?(Number(x)/3600).toFixed(1)+' ч':Math.max(1,Math.round(Number(x)/60))+' мин';const fmtTime=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';return positions.map(z=>{const pnl=Number(z.unrealized_pnl_rub||0);const metricLabel=z.entry_metric_label==='PROBABILITY'?'Prob-ty':z.entry_metric_label==='MODEL_SCORE'?'Model score':z.entry_metric_label==='SIGNAL_STRENGTH'?'Signal':'Metric';const metric=z.entry_metric_value==null?'—':(100*Number(z.entry_metric_value)).toFixed(1)+'%';const slp=z.stop_distance_pct==null?'—':Number(z.stop_distance_pct).toFixed(2)+'%';const tpp=z.take_distance_pct==null?'—':Number(z.take_distance_pct).toFixed(2)+'%';const rr=z.current_rr==null?'—':Number(z.current_rr).toFixed(2)+'x';const marker=Math.max(4,Math.min(96,Number(z.risk_bar_position_pct??50)));return `<div class="assetview position-card position-card-v2"><div class="assetview-head position-head"><div><b>${z.portfolio} · ${z.asset}</b><div class="position-meta">${z.horizon||'—'} · ${z.setup||'—'} · ${z.regime||'—'}</div></div><b class="${z.direction==='LONG'?'ok':'bad'}">${z.direction} · ${(100*Number(z.target_fraction||0)).toFixed(0)}%</b></div><div class="position-kpis"><div class="position-kpi"><span>Вход</span><b>${fmtPx(z.avg_entry_price)}</b></div><div class="position-kpi"><span>Текущая</span><b>${fmtPx(z.last_price)}</b></div><div class="position-kpi position-pnl"><span>P/L</span><b class="${pnl>0?'ok':pnl<0?'bad':''}">${rub(z.unrealized_pnl_rub)} · ${z.unrealized_return_pct==null?'—':Number(z.unrealized_return_pct).toFixed(2)+'%'}</b></div></div><div class="position-risk"><div class="position-risk-grid"><div><span>SL</span><b>${fmtPx(z.stop_price)}</b><small>до SL ${slp}</small></div><div><span>TP</span><b>${fmtPx(z.take_price)}</b><small>до TP ${tpp}</small></div><div><span>R/R</span><b>${rr}</b><small>${z.take_price_source||'—'}</small></div></div><div class="position-riskbar"><span>SL</span><div class="position-risk-track"><i style="left:${marker}%"></i></div><span>TP</span></div></div><div class="position-foot"><div><span>Объём</span><b>${rub(z.notional_rub)} / ${fmtUsd(z.notional_usd)}</b></div><div><span>Кол-во</span><b>${['BTC','ETH'].includes(z.asset)?Number(z.units||0).toFixed(4):Number(z.units||0).toLocaleString('ru-RU',{maximumFractionDigits:2})}</b></div><div><span>${metricLabel}</span><b title="${z.entry_metric_source||'—'}">${metric}</b></div><div><span>Удержание</span><b>${fmtHold(z.held_seconds)}</b></div><div><span>Открыта</span><b>${fmtTime(z.opened_at)}</b></div><div><span>Обновлено</span><b>${fmtTime(z.last_mark_at)}</b></div></div></div>`}).join('')})():'Открытых позиций нет — портфели в cash.';const trades="""
    
    pattern = r"""posel\.innerHTML=positions\.length\?positions\.map\(z=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Открытых позиций[^']*';const trades="""
    value, count = re.subn(pattern, replacement, value, count=1, flags=re.S)
    if count != 1:
        print(json.dumps({'event':'V90_UI_PATCH','status':'error','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    else:
        print(json.dumps({'event':'V90_UI_PATCH','status':'ok','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    
    trade_replacement = """if(!trel.dataset.v90Managed){trel.innerHTML='<span class="stamp">Журнал закрытых сделок загружается…</span>'}"""
    trade_pattern = r"""trel\.innerHTML=trades\.length\?trades\.slice\(0,(?:30|40)\)\.map\(t=>`<div class="assetview(?: tradecompact)?">.*?</div></div>`\)\.join\(''\):'Сделок в журнале пока нет\.'"""
    value, trade_count = re.subn(trade_pattern, trade_replacement, value, count=1, flags=re.S)
    print(json.dumps({'event':'V90_CLOSED_TRADE_UI_PATCH','replacements':trade_count,
                      'status':'legacy_replaced' if trade_count==1 else 'legacy_unused'},ensure_ascii=False,separators=(',',':')),flush=True)
    if False and trade_count != 1:
        closed_fallback = r"""<script>
    (function(){
     const ORDER=['Champion','Challenger','Impulse','Aggressive'];
     const SHORT={
      'DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH':'DIR/ENTRY FAIL',
      'FAVORABLE_PATH_NOT_MONETIZED':'MOVE NOT CAPTURED',
      'RIGHT_DIRECTION_HIGH_CAPTURE':'HIGH CAPTURE',
      'RIGHT_DIRECTION_LOW_CAPTURE':'LOW CAPTURE',
      'MIXED_EXECUTION':'MIXED EXEC',
      'RECOVERED_HISTORICAL_NO_LEARNING':'RECOVERED'
     };
     const rubv=v=>Number(v||0).toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽';
     const fmtPx=x=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:3});
     const fmtTime=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
     const fmtHold=x=>x==null?'—':(Number(x)>=3600?(Number(x)/3600).toFixed(1)+'ч':Math.max(1,Math.round(Number(x)/60))+'м');
     const pct=x=>x==null?'—':Number(x).toFixed(2)+'%';
     function card(t){
       const pnl=Number(t.net_pnl_rub||0), cost=Number(t.fees_rub||0)+Number(t.funding_rub||0);
       const prob=t.entry_probability==null?'—':(100*Number(t.entry_probability)).toFixed(1)+'%';
       const label=SHORT[t.learning_label]||t.learning_label||'—';
       const lesson=String(t.learning_conclusion||'—');
       return `<div class="assetview closed-trade-card" onclick="this.classList.toggle('expanded')">
    <div class="closed-trade-head"><b>${t.asset||'—'} · ${t.direction||'—'}</b><b class="${pnl>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rubv(t.net_pnl_rub)} · ${t.return_pct==null?'—':Number(t.return_pct).toFixed(2)+'%'}</b></div>
    <div class="closed-mainline"><span>${fmtPx(t.avg_entry_price)} → ${fmtPx(t.avg_exit_price)}</span><span>G <b>${t.gross_pnl_rub==null?'—':rubv(t.gross_pnl_rub)}</b></span><span>C <b>${rubv(cost)}</b></span><span>${t.horizon||'—'} · ${fmtHold(t.held_seconds)}</span></div>
    <div class="closed-mainline"><span>MFE <b>${pct(t.mfe_pct)}</b></span><span>MAE <b>${pct(t.mae_pct)}</b></span><span>Exit <b>${t.exit_reason||'—'}</b></span><span>Prob <b>${prob}</b></span></div>
    <div class="closed-lesson"><span class="learn-dot">●</span><b>${label}</b><span>${lesson}</span></div>
    <div class="closed-extra"><span>Открыта <b>${fmtTime(t.opened_at)}</b></span><span>Закрыта <b>${fmtTime(t.closed_at)}</b></span><span>QTY <b>${t.quantity==null?'—':Number(t.quantity).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></span><span>SL/TP <b>${fmtPx(t.stop_price)} / ${fmtPx(t.take_price)}</b></span><span>${t.setup||'—'} · ${t.regime||'—'}</span><span>Funding <b>${rubv(t.funding_rub||0)}</b></span></div>
       </div>`;
     }
     function group(name,rows){
       const wins=rows.filter(t=>Number(t.net_pnl_rub||0)>0).length,net=rows.reduce((a,t)=>a+Number(t.net_pnl_rub||0),0),wr=rows.length?100*wins/rows.length:0;
       const id='cg_'+name.replace(/[^a-z0-9]/gi,'_');
       const initial=rows, hidden=0;
       return `<div class="closed-portfolio" id="${id}"><div class="closed-portfolio-summary"><b>${name}</b><span>· ${rows.length}</span><span>· ${wins} win</span><span>· ${wr.toFixed(1)}%</span><b class="${net>=0?'ok':'bad'}">${rubv(net)}</b></div><div class="closed-list">${initial.map(card).join('')}</div>${hidden?`<button class="closed-more-btn" data-group="${name.replace(/"/g,'&quot;')}">Ещё ${hidden}</button>`:''}</div>`;
     }
     function learningLine(ls,d){
       if(!ls||ls.status!=='OK')return '';
       return `<div class="closed-learning-status">Learning · ${ls.lessons_written||0}/${d.learning_eligible_closed_count||0} lessons · ${ls.unique_market_ideas||0} market episodes · applied ${ls.applications||0} · actionable ${ls.actionable_contexts||0} · validated ${ls.validated_rules||0}</div>`;
     }
     function render(d){
       const trades=Array.isArray(d.trades)?d.trades:[];
       if(!trades.length)return 'Закрытых сделок пока нет.';
       const groups={};trades.forEach(t=>{const k=t.portfolio_name||'—';(groups[k]||(groups[k]=[])).push(t)});
       const keys=Object.keys(groups).sort((a,b)=>{const ia=ORDER.indexOf(a),ib=ORDER.indexOf(b);return (ia<0?999:ia)-(ib<0?999:ib)||a.localeCompare(b)});
       return learningLine(d.learning_status,d)+keys.map(k=>group(k,groups[k])).join('');
     }
     let observer=null,timer=null,lastData=null;
     function bindMore(el){
       el.querySelectorAll('.closed-more-btn').forEach(btn=>btn.onclick=function(ev){
     ev.stopPropagation();const name=this.dataset.group, rows=(lastData.trades||[]).filter(t=>(t.portfolio_name||'—')===name);
     const host=this.closest('.closed-portfolio');host.querySelector('.closed-list').innerHTML=rows.map(card).join('');this.remove();
       });
     }
     async function refreshClosed(){
       const el=document.getElementById('portfoliotrades');if(!el)return;
       try{
     const r=await fetch('/api/v1/portfolio-trades',{cache:'no-store'}),d=await r.json();lastData=d;
     if(observer)observer.disconnect();el.innerHTML=render(d);bindMore(el);
       }catch(e){}
       finally{if(observer)observer.observe(el,{childList:true,subtree:true,characterData:true})}
     }
     function start(){
       const el=document.getElementById('portfoliotrades');if(!el)return;
       observer=new MutationObserver(()=>{clearTimeout(timer);timer=setTimeout(refreshClosed,180)});
       observer.observe(el,{childList:true,subtree:true,characterData:true});
       setTimeout(refreshClosed,10000);setInterval(refreshClosed,120000);
     }
     if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
    })();
    </script>"""
        value = value.replace('</body>', closed_fallback + '</body>')
        print(json.dumps({'event':'V90_CLOSED_TRADE_UI_FALLBACK','status':'installed'},
                         ensure_ascii=False,separators=(',',':')),flush=True)
    cold_start_retry = r"""<script id="V90_COLD_START_RETRY">
    (function(){
     let n=0;
     async function retry(){
       if(n>=12)return;
       const sys=document.getElementById('sys');
       const txt=(sys&&sys.textContent||'').trim().toUpperCase();
       const stamp=(document.getElementById('stamp')?.textContent||'').toUpperCase();
       if(txt==='UPDATING'||txt==='DEGRADED'||stamp.includes('HTTP 500')||stamp.includes('ЗАДЕРЖАНО')){
     n++;
     try{if(typeof load==='function')await load();}catch(e){}
     try{if(typeof loadPortfolios==='function')await loadPortfolios();}catch(e){}
     setTimeout(retry,3500);
       }
     }
     if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(retry,1800),{once:true});
     else setTimeout(retry,1800);
    })();
    </script>"""
    value=value.replace('</body>',cold_start_retry+'</body>')
    deep_tab_refresh = r"""<script id="V90_DEEP_TAB_REFRESH">
    (function(){
     function wire(){
       document.querySelectorAll('.nav button').forEach(function(b){
     b.addEventListener('click',function(){
       if(b.dataset.view==='research'||b.dataset.view==='system'){
         try{ if(typeof load==='function') load(); }catch(e){}
       }
     });
       });
     }
     if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',wire,{once:true});else wire();
    })();
    </script>"""
    value=value.replace('</body>',deep_tab_refresh+'</body>')
    top_metric_layout = r"""<script id="V90_TOP_METRIC_LAYOUT">
    (function(){
     function install(){
       const market=document.getElementById('market');
       const sys=document.getElementById('sys'), users=document.getElementById('users');
       const src=document.getElementById('src'), rules=document.getElementById('rules'), mgr=document.getElementById('mgr');
       if(!market||!sys||!users||!src||!rules||!mgr)return;
    
       const sysCard=sys.closest('.card'), userCard=users.closest('.card');
       const srcCard=src.closest('.card'), rulesCard=rules.closest('.card'), mgrCard=mgr.closest('.card');
       if(!sysCard||!userCard||!srcCard||!rulesCard||!mgrCard)return;
    
       // Show only the health value; the "Статус" label is intentionally removed.
       const sysLabel=sysCard.querySelector('.k');
       if(sysLabel){sysLabel.textContent='';sysLabel.style.display='none';}
       sysCard.classList.add('v86-health-card');
    
       let statusRow=document.getElementById('v86-status-users-row');
       if(!statusRow){
     statusRow=document.createElement('div');
     statusRow.id='v86-status-users-row';
     statusRow.className='v86-top-row v86-status-users-row';
     market.insertBefore(statusRow,sysCard);
       }
       if(sysCard.parentElement!==statusRow)statusRow.appendChild(sysCard);
       if(userCard.parentElement!==statusRow)statusRow.appendChild(userCard);
    
       let knowledgeRow=document.getElementById('v86-knowledge-row');
       if(!knowledgeRow){
     knowledgeRow=document.createElement('div');
     knowledgeRow.id='v86-knowledge-row';
     knowledgeRow.className='v86-top-row v86-knowledge-row';
     const depth=document.getElementById('capacity');
     const depthCard=depth&&depth.closest('.card');
     if(depthCard&&depthCard.nextSibling)market.insertBefore(knowledgeRow,depthCard.nextSibling);
     else market.insertBefore(knowledgeRow,market.firstChild);
       }
       [srcCard,rulesCard,mgrCard].forEach(c=>{if(c.parentElement!==knowledgeRow)knowledgeRow.appendChild(c)});
     }
     if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',install,{once:true});else install();
    })();
    </script>"""
    value=value.replace('</body>',top_metric_layout+'</body>')
    first_screen_script = r"""<script id="V90_FIRST_SCREEN_LIVE_USERS">
    (function(){
     const key='veritas_visitor';
     let vid=localStorage.getItem(key);
     if(!vid){vid=(crypto.randomUUID?crypto.randomUUID():(Date.now()+'-'+Math.random()));localStorage.setItem(key,vid)}
     async function refreshUsers(){
       try{
     const r=await fetch('/api/v1/presence',{headers:{'X-Veritas-Visitor':vid},cache:'no-store'});
     if(!r.ok)return;
     const d=await r.json();
     const el=document.getElementById('users'), sm=document.getElementById('userssmall');
     if(el)el.textContent=`${d.online_users??0} / ${d.unique_users??0}`;
     if(sm)sm.textContent='онлайн сейчас / уникальных';
       }catch(e){}
     }
     if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',refreshUsers,{once:true});else refreshUsers();
     setInterval(refreshUsers,45000);
    })();
    </script>"""
    value=value.replace('</body>',first_screen_script+'</body>')
    compact_css = """<style>
    .top h1{
      color:#93A4B3;
      letter-spacing:.7px;
      font-weight:800;
      text-shadow:0 0 16px rgba(147,164,179,.20);
    }
    .top h1::after{
      content:' · 9.0';
      color:#657482;
      font-size:.46em;
      font-weight:700;
      letter-spacing:.35px;
      vertical-align:middle;
    }
    #portfoliopositions .position-card{padding:8px 11px;margin:0 0 6px;border-radius:12px}
    #portfoliopositions .position-head{margin-bottom:6px;align-items:center}
    #portfoliopositions .position-head b{font-size:13px;line-height:1.05}
    #portfoliopositions .position-columns{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,.95fr);gap:18px;width:100%}
    #portfoliopositions .position-col{display:flex;flex-direction:column;gap:3px;min-width:0}
    #portfoliopositions .position-col>div{display:grid;grid-template-columns:64px minmax(0,1fr);align-items:baseline;column-gap:7px;white-space:nowrap;min-width:0}
    #portfoliopositions .position-col span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.2px}
    #portfoliopositions .position-col b{font-size:12px;line-height:1.15;overflow:hidden;text-overflow:ellipsis;text-align:left}
    #portfoliopositions .position-left .position-gap{margin-top:7px}
    #portfoliopositions .position-card-v2{padding:7px 9px;margin-bottom:5px;border-radius:10px}
    #portfoliopositions .position-card-v2 .position-head{margin-bottom:5px}
    #portfoliopositions .position-card-v2 .position-head>div{min-width:0}
    #portfoliopositions .position-meta{margin-top:2px;font-size:7.3px;line-height:1.15;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #portfoliopositions .position-kpis{display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:5px;margin-bottom:5px}
    #portfoliopositions .position-kpi{padding:4px 6px;border:1px solid var(--border);border-radius:7px;min-width:0}
    #portfoliopositions .position-kpi span,#portfoliopositions .position-risk-grid span,#portfoliopositions .position-foot span{display:block;font-size:7px;color:var(--muted);text-transform:uppercase;letter-spacing:.12px}
    #portfoliopositions .position-kpi b{display:block;margin-top:1px;font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    #portfoliopositions .position-pnl b{font-size:11.5px}
    #portfoliopositions .position-risk{padding:5px 6px;border:1px solid var(--border);border-radius:8px;margin-bottom:5px}
    #portfoliopositions .position-risk-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px}
    #portfoliopositions .position-risk-grid b{display:block;margin-top:1px;font-size:9.5px}
    #portfoliopositions .position-risk-grid small{display:block;margin-top:0;font-size:6.8px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #portfoliopositions .position-riskbar{display:grid;grid-template-columns:auto 1fr auto;gap:5px;align-items:center;margin-top:4px;font-size:6.8px;color:var(--muted)}
    #portfoliopositions .position-risk-track{position:relative;height:3px;border-radius:999px;background:linear-gradient(90deg,rgba(239,103,103,.65),rgba(143,154,164,.20),rgba(89,214,148,.65))}
    #portfoliopositions .position-risk-track i{position:absolute;top:50%;width:6px;height:6px;border-radius:50%;transform:translate(-50%,-50%);background:var(--text);box-shadow:0 0 0 1px rgba(143,154,164,.22)}
    #portfoliopositions .position-foot{display:grid;grid-template-columns:1.35fr .7fr .85fr .7fr .9fr .9fr;gap:4px}
    #portfoliopositions .position-foot>div{min-width:0}
    #portfoliopositions .position-foot b{display:block;margin-top:1px;font-size:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #portfoliotrades .closed-learning-status{font-size:9px;line-height:1.1;color:var(--muted);padding:0 2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #portfoliotrades .closed-portfolio{margin:0 0 8px}
    #portfoliotrades .closed-portfolio-summary{display:flex;align-items:baseline;gap:4px;flex-wrap:wrap;padding:1px 2px 4px;font-size:10px;line-height:1.1;color:var(--muted)}
    #portfoliotrades .closed-portfolio-summary>b:first-child{font-size:11px;color:var(--text)}
    #portfoliotrades .closed-list{display:flex;flex-direction:column;gap:3px}
    #portfoliotrades .closed-trade-card{padding:5px 8px;margin:0;border-radius:9px;cursor:pointer}
    #portfoliotrades .closed-trade-head{display:flex;justify-content:space-between;gap:7px;align-items:baseline;margin-bottom:1px}
    #portfoliotrades .closed-trade-head b{font-size:10px;line-height:1.05}
    #portfoliotrades .closed-mainline{display:flex;gap:3px 6px;align-items:baseline;min-width:0;font-size:8.3px;line-height:1.08;color:var(--muted);white-space:nowrap;overflow:hidden}
    #portfoliotrades .closed-mainline span{overflow:hidden;text-overflow:ellipsis}
    #portfoliotrades .closed-mainline b{font-size:8.4px;color:var(--text)}
    #portfoliotrades .closed-lesson{display:flex;gap:4px;align-items:baseline;margin-top:2px;padding-top:2px;border-top:1px solid var(--border);font-size:8px;line-height:1.08;color:var(--muted);min-width:0}
    #portfoliotrades .closed-lesson b{font-size:8px;color:var(--text);white-space:nowrap}
    #portfoliotrades .closed-lesson span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #portfoliotrades .learn-dot{color:#ef6767;font-size:8px;flex:0 0 auto}
    #portfoliotrades .closed-extra{display:none;gap:4px 8px;flex-wrap:wrap;margin-top:3px;padding-top:3px;border-top:1px dashed var(--border);font-size:7.8px;color:var(--muted)}
    #portfoliotrades .closed-extra b{font-size:7.9px;color:var(--text)}
    #portfoliotrades .closed-trade-card.expanded .closed-extra{display:flex}
    #portfoliotrades .closed-more-btn{width:100%;margin-top:3px;padding:4px 6px;border:1px solid var(--border);border-radius:8px;background:transparent;color:var(--muted);font-size:8.5px}
    @media(max-width:700px){
     #portfoliopositions .position-card{padding:7px 9px;margin-bottom:5px}
     #portfoliopositions .position-head{margin-bottom:5px}
     #portfoliopositions .position-head b{font-size:11.5px}
     #portfoliopositions .position-columns{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:12px}
     #portfoliopositions .position-col{gap:2px}
     #portfoliopositions .position-col>div{grid-template-columns:53px minmax(0,1fr);column-gap:5px}
     #portfoliopositions .position-col span{font-size:8px}
     #portfoliopositions .position-col b{font-size:10.5px}
     #portfoliopositions .position-left .position-gap{margin-top:6px}
     #portfoliopositions .position-card-v2{padding:6px 7px;margin-bottom:4px}
     #portfoliopositions .position-kpis{grid-template-columns:1fr 1fr;margin-bottom:4px;gap:4px}
     #portfoliopositions .position-pnl{grid-column:span 2}
     #portfoliopositions .position-risk-grid{grid-template-columns:1fr 1fr 1fr;gap:4px}
     #portfoliopositions .position-foot{grid-template-columns:1.2fr .8fr 1fr;gap:4px 6px}
     #portfoliotrades .closed-learning-status{font-size:7.6px;padding-bottom:4px}
     #portfoliotrades .closed-portfolio-summary{font-size:8.4px;gap:3px;padding-bottom:3px}
     #portfoliotrades .closed-portfolio-summary>b:first-child{font-size:9.5px}
     #portfoliotrades .closed-trade-card{padding:5px 7px}
     #portfoliotrades .closed-trade-head b{font-size:9.2px}
     #portfoliotrades .closed-mainline{font-size:7.4px;gap:2px 4px}
     #portfoliotrades .closed-mainline b{font-size:7.5px}
     #portfoliotrades .closed-lesson{font-size:7.2px}
     #portfoliotrades .closed-lesson b{font-size:7.2px}
     #portfoliotrades .closed-extra{font-size:7px}
     #portfoliotrades .closed-more-btn{font-size:7.5px;padding:3px 5px}
    }
    .veritas-brandlock{display:inline-flex;align-items:flex-start;min-width:0}
    .veritas-primary{display:inline-flex;flex-direction:column;align-items:stretch;min-width:0}
    .veritas-core{display:flex;align-items:flex-start;gap:14px;min-width:0}
    .veritas-right{display:inline-flex;flex-direction:column;align-items:flex-start;min-width:0}
    .veritas-logo-img{width:78px;height:90px;flex:0 0 78px;object-fit:contain;object-position:center;filter:drop-shadow(0 10px 24px rgba(0,0,0,.28))}
    .veritas-wordmark{display:flex;align-items:center;height:64px;white-space:nowrap;font-family:"Avenir Next","Century Gothic","Helvetica Neue","Segoe UI",Arial,sans-serif;font-size:53px;font-weight:360;letter-spacing:.17em;line-height:1;color:#e0e5e9}
    .veritas-wordmark .wm-v{margin-right:.01em}
    .veritas-wordmark .wm-e{display:inline-flex;width:.72em;height:.94em;flex-direction:column;justify-content:space-between;align-self:center;margin-right:.10em;transform:translateY(0)}
    .veritas-wordmark .wm-e i{display:block;width:100%;height:.135em;background:#86b39a;border-radius:.025em;box-shadow:0 0 0 .01em rgba(255,255,255,.035)}
    .markets-word{margin-top:3px;font-family:"Avenir Next","Century Gothic","Helvetica Neue","Segoe UI",Arial,sans-serif;font-size:20px;font-weight:360;letter-spacing:.12em;line-height:1;color:#929da6;white-space:nowrap}
    .veritas-subtitle{width:100%;margin-top:9px;color:#98a2aa;font-family:"Avenir Next","Helvetica Neue","Segoe UI",Arial,sans-serif;font-size:11px;font-weight:420;letter-spacing:.09em;line-height:1.15;white-space:nowrap;text-align:justify;text-align-last:justify}
    .veritas-subtitle::after{content:"";display:inline-block;width:100%}
    @media(max-width:900px){
     .veritas-core{gap:10px}
     .veritas-logo-img{width:58px;height:68px;flex-basis:58px}
     .veritas-wordmark{height:48px;font-size:36px;font-weight:380;letter-spacing:.125em}
     .veritas-wordmark .wm-e{height:.95em;width:.72em;margin-right:.09em}
     .veritas-wordmark .wm-e i{height:.14em}
     .markets-word{margin-top:2px;font-size:14px;letter-spacing:.10em}
     .veritas-subtitle{margin-top:6px;font-size:8px;font-weight:430;letter-spacing:.065em}
    }
    .v86-health-card{display:flex!important;align-items:center!important;justify-content:center!important;padding:12px 16px!important}
    .v86-health-card .v{grid-column:auto!important;grid-row:auto!important;text-align:center!important;font-size:22px!important}
    .v86-top-row{grid-column:span 12;display:grid;gap:10px;min-width:0}
    .v86-status-users-row{grid-template-columns:repeat(2,minmax(0,1fr))}
    .v86-knowledge-row{grid-template-columns:repeat(3,minmax(0,1fr))}
    .v86-top-row>.card{grid-column:auto!important;margin:0;min-width:0}
    .v86-status-users-row>.card{display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-rows:auto auto;column-gap:14px;align-items:center;padding:12px 16px}
    .v86-status-users-row>.card .k{grid-column:1;grid-row:1 / span 2;margin:0;font-size:11px;white-space:nowrap}
    .v86-status-users-row>.card .v{grid-column:2;grid-row:1;font-size:24px;line-height:1;text-align:right;white-space:nowrap}
    .v86-status-users-row>.card .stamp{grid-column:2;grid-row:2;text-align:right;font-size:9px;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .v86-knowledge-row>.card{padding:11px 12px;display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-rows:auto auto;column-gap:8px;align-items:center}
    .v86-knowledge-row>.card .k{grid-column:1;grid-row:1;font-size:10px;line-height:1.1;white-space:normal}
    .v86-knowledge-row>.card .v{grid-column:2;grid-row:1;font-size:21px;line-height:1;text-align:right;white-space:nowrap}
    .v86-knowledge-row>.card .stamp{grid-column:1 / span 2;grid-row:2;margin-top:4px;font-size:8px;line-height:1.05;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    @media(max-width:900px){
     .v86-top-row{gap:6px}
     .v86-status-users-row{grid-template-columns:repeat(2,minmax(0,1fr))}
     .v86-knowledge-row{grid-template-columns:repeat(3,minmax(0,1fr))}
     .v86-status-users-row>.card{padding:9px 10px;column-gap:7px}
     .v86-status-users-row>.card .k{font-size:8px}
     .v86-status-users-row>.card .v{font-size:18px}
     .v86-status-users-row>.card .stamp{font-size:7px}
     .v86-knowledge-row>.card{padding:9px 8px;display:block;text-align:left}
     .v86-knowledge-row>.card .k{font-size:7.5px;min-height:18px;display:flex;align-items:flex-start}
     .v86-knowledge-row>.card .v{font-size:17px;margin-top:4px;text-align:left}
     .v86-knowledge-row>.card .stamp{font-size:6.8px;margin-top:3px;white-space:normal;line-height:1.15}
    }
    </style>"""
    value = value.replace('</head>', compact_css + '</head>')
    brand_fix_css = """<style id="V90_BRAND_VISUAL_FIX">
    .veritas-brandlock{
      display:inline-flex!important;
      flex-direction:column!important;
      align-items:flex-start!important;
      width:auto!important;
      max-width:100%!important;
    }
    .veritas-core{
      display:flex!important;
      align-items:center!important;
      gap:18px!important;
      min-width:0!important;
    }
    .veritas-logo-img{
      width:84px!important;
      height:84px!important;
      flex:0 0 84px!important;
      display:block!important;
      overflow:visible!important;
      filter:drop-shadow(0 10px 22px rgba(0,0,0,.32));
    }
    .veritas-right{
      display:flex!important;
      flex-direction:column!important;
      align-items:flex-start!important;
      min-width:0!important;
    }
    .veritas-wordmark{
      display:flex!important;
      align-items:center!important;
      height:58px!important;
      margin:0!important;
      white-space:nowrap!important;
      font-family:"Avenir Next","Helvetica Neue",-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif!important;
      font-size:50px!important;
      font-weight:500!important;
      letter-spacing:.145em!important;
      line-height:1!important;
      color:#edf1f4!important;
      text-rendering:geometricPrecision!important;
      -webkit-font-smoothing:antialiased!important;
      text-shadow:0 0 14px rgba(211,222,230,.035)!important;
    }
    .veritas-wordmark .wm-v{margin-right:.015em!important}
    .veritas-wordmark .wm-rest{font-weight:470!important}
    .veritas-wordmark .wm-e{
      display:inline-flex!important;
      width:.70em!important;
      height:.83em!important;
      flex-direction:column!important;
      justify-content:space-between!important;
      align-self:center!important;
      margin:0 .105em 0 .01em!important;
      transform:translateY(.01em)!important;
    }
    .veritas-wordmark .wm-e i{
      display:block!important;
      width:100%!important;
      height:.135em!important;
      background:linear-gradient(90deg,#7fa58f,#a8cbb6)!important;
      border-radius:1.5px!important;
      box-shadow:0 0 10px rgba(145,189,163,.08)!important;
    }
    .markets-word{
      margin-top:5px!important;
      font-family:"Avenir Next","Helvetica Neue",-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif!important;
      font-size:20px!important;
      font-weight:450!important;
      letter-spacing:.16em!important;
      line-height:1!important;
      color:#aeb8c1!important;
      opacity:1!important;
      -webkit-font-smoothing:antialiased!important;
    }
    .veritas-subtitle{
      width:auto!important;
      margin-top:15px!important;
      padding:0!important;
      color:#8f9aa4!important;
      font-family:"Avenir Next","Helvetica Neue",-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif!important;
      font-size:11px!important;
      font-weight:500!important;
      letter-spacing:.115em!important;
      line-height:1.15!important;
      text-transform:uppercase!important;
      white-space:nowrap!important;
      text-align:left!important;
      text-align-last:auto!important;
      display:block!important;
      -webkit-font-smoothing:antialiased!important;
    }
    .veritas-subtitle::after{display:none!important;content:none!important}
    @media(max-width:900px){
      .veritas-core{gap:12px!important}
      .veritas-logo-img{width:64px!important;height:64px!important;flex-basis:64px!important}
      .veritas-wordmark{height:46px!important;font-size:37px!important;font-weight:500!important;letter-spacing:.12em!important}
      .markets-word{margin-top:3px!important;font-size:15px!important;font-weight:450!important;letter-spacing:.145em!important}
      .veritas-subtitle{margin-top:10px!important;font-size:8.4px!important;font-weight:520!important;letter-spacing:.085em!important}
    }
    @media(max-width:560px){
      .veritas-core{gap:10px!important}
      .veritas-logo-img{width:56px!important;height:56px!important;flex-basis:56px!important}
      .veritas-wordmark{height:40px!important;font-size:32px!important;letter-spacing:.105em!important}
      .markets-word{font-size:13px!important}
      .veritas-subtitle{font-size:7.5px!important;letter-spacing:.065em!important}
    }
    </style>"""
    value = value.replace('</head>', brand_fix_css + '</head>')
    supplied_brand_css = """<style id="V90_SUPPLIED_BRAND_ARTWORK">
    .veritas-brand-artwork{display:block!important;width:min(640px,92vw)!important;max-width:100%!important;margin:0!important;padding:0!important}
    .veritas-brand-image{display:block!important;width:100%!important;height:auto!important;max-width:640px!important;object-fit:contain!important;object-position:left center!important;border:0!important;filter:none!important}
    @media(max-width:900px){.veritas-brand-artwork{width:min(500px,94vw)!important}.veritas-brand-image{max-width:500px!important}}
    @media(max-width:560px){.veritas-brand-artwork{width:96vw!important}.veritas-brand-image{width:100%!important;max-width:none!important}}
    </style>"""
    value = value.replace('</head>', supplied_brand_css + '</head>')

    
    value = value.replace(' · max gross ${Number(pd.max_gross||0).toFixed(1)}×', ' · max gross: Aggressive 5.0× · остальные 2.0×')
    value = value.replace('Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Champion, Challenger, Impulse и Aggressive. Реальные деньги не используются.',
                          'Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Импульсный, Агрессивный, Чемпион и Челленджер. Реальные деньги не используются.')
    value = value.replace('V86','V90').replace('v86','v90')
    nq_label_js = r"""<script id="V90_NQ_FUTURES_LABEL">
    (function(){
      const label=a=>a==='NQ'?'NDXf':a;
      const oldRender=window.renderMatrix;
      if(typeof oldRender==='function'){
        window.renderMatrix=function(rows){
          oldRender(rows);
          document.querySelectorAll('#matrix .asset-name').forEach(el=>{
            if(el.textContent.trim()==='NQ')el.textContent='NDXf';
          });
          document.querySelectorAll('#superstrip .superasset').forEach(el=>{
            el.childNodes.forEach(n=>{
              if(n.nodeType===Node.TEXT_NODE && n.textContent.trim()==='NQ')n.textContent='NDXf';
            });
          });
        };
      }
      const observer=new MutationObserver(()=>{
        document.querySelectorAll('.asset-name,.assetview-name,.superasset,b').forEach(el=>{
          const t=el.textContent.trim();
          if(t==='NQ')el.textContent='NDXf';
          else if(t.startsWith('NQ ·'))el.textContent=t.replace(/^NQ\s*·/,'NDXf ·');
          else if(t.includes('· NQ ·'))el.textContent=t.replace('· NQ ·','· NDXf ·');
        });
      });
      observer.observe(document.body,{subtree:true,childList:true});
    })();
    </script>"""
    value = value.replace('</body>', nq_label_js + '</body>')
    # VERITAS V90 R17 MARKET VIEW
    value = value.replace(
        "const tfOrder=['1h','4h','1d','3d','7d'],assets=['BTC','ETH','NDX','BRENT','GOLD','MOEX','CNYRUBF'];",
        "const tfOrder=['5m','1h','4h','1d','3d','7d'],assets=['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'];"
    )
    value = value.replace(
        "<th>Актив</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th>",
        "<th>Актив</th><th>5м</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th>"
    )
    value = value.replace(
        "Цифровой инвестиционный комитет · BTC / ETH / NDX / Brent / Gold / MOEX",
        "Цифровой инвестиционный комитет · BTC / ETH / NDXf / Brent / Gold / MOEX / CNYRUBf"
    )
    value = value.replace(
        "Эпизоды, а не повторяющиеся 5-минутные снимки. v70 пока оценивается в shadow.",
        "Статистика считается по независимым рыночным эпизодам; повторные циклы не дублируют опыт."
    )

    dense_market_css = """<style id="V90_R17_MARKET_VIEW">
    #thesis{line-height:1.25!important}
    .v90-overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
    .v90-overview-row{border:1px solid var(--border);border-radius:9px;padding:7px 8px;min-width:0;background:rgba(255,255,255,.012)}
    .v90-overview-head{display:grid;grid-template-columns:64px 64px 1fr auto;gap:6px;align-items:center;margin-bottom:5px}
    .v90-overview-asset{font-size:11px;font-weight:800}
    .v90-overview-dir{font-size:9px;font-weight:800}
    .v90-overview-regime{font-size:8px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .v90-overview-price{font-size:10px;font-weight:750;text-align:right}
    .v90-overview-tfs{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:3px;margin-bottom:5px}
    .v90-overview-tf{padding:3px 2px;border-radius:5px;border:1px solid var(--border);text-align:center;font-size:7px;line-height:1.15}
    .v90-overview-tf b{display:block;font-size:8px}
    .v90-overview-foot{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;font-size:7.5px;color:var(--muted)}
    .v90-overview-foot b{color:var(--text);font-size:8px}
    @media(max-width:900px){.v90-overview-grid{grid-template-columns:1fr}.v90-overview-head{grid-template-columns:56px 58px 1fr auto}}
    </style>"""
    value=value.replace('</head>',dense_market_css+'</head>')

    dense_market_js = r"""<script id="V90_R17_DENSE_ASSET_OVERVIEW">
    (function(){
      const TF=['5m','1h','4h','1d','3d','7d'];
      const AS=['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'];
      const label=a=>a==='NQ'?'NDXf':a==='CNYRUBF'?'CNYRUBf':a;
      const dir=x=>{
        const d=(x&&x.research_decision)||x?.decision||'NO_TRADE';
        return d==='LONG'?'LONG':d==='SHORT'?'SHORT':'WAIT';
      };
      const cls=d=>d==='LONG'?'ok':d==='SHORT'?'bad':'warn';
      const arrow=d=>d==='LONG'?'↑':d==='SHORT'?'↓':'→';
      const fmt=v=>{
        const n=Number(v);if(!Number.isFinite(n))return '—';
        return n>=1000?n.toLocaleString('en-US',{maximumFractionDigits:2}):n.toLocaleString('en-US',{maximumFractionDigits:4});
      };
      window.renderDenseAssetOverview=function(rows){
        const el=document.getElementById('thesis');if(!el)return;
        rows=Array.isArray(rows)?rows:[];
        const map={};rows.forEach(x=>{if(x?.asset&&x?.horizon)map[x.asset+'|'+x.horizon]=x});
        const html=AS.map(a=>{
          const xs=TF.map(tf=>map[a+'|'+tf]).filter(Boolean);
          const live5=map[a+'|5m'];
          const best=xs.slice().sort((p,q)=>Number(q.confidence||0)-Number(p.confidence||0))[0]||{};
          const dirs=xs.map(dir);
          const longN=dirs.filter(x=>x==='LONG').length, shortN=dirs.filter(x=>x==='SHORT').length;
          const consensus=longN>shortN?'LONG':shortN>longN?'SHORT':'WAIT';
          const regime=(live5?.regime||best.regime||'—');
          const price=live5?.price??best.price;
          const indep=Math.max(0,...xs.map(x=>Number(x.independent_evidence_families||0)));
          const bestRR=Math.max(0,...xs.map(x=>Number(x.expected_to_stop_ratio||0)).filter(Number.isFinite));
          const execN=xs.filter(x=>x.execution_eligible===true).length;
          const tfhtml=TF.map(tf=>{
            const x=map[a+'|'+tf];
            if(!x)return '<div class="v90-overview-tf"><b>'+tf+'</b>—</div>';
            const d=dir(x);
            return '<div class="v90-overview-tf '+cls(d)+'"><b>'+tf+'</b>'+arrow(d)+' '+(Number(x.confidence||0)*100).toFixed(0)+'%</div>';
          }).join('');
          return '<div class="v90-overview-row">'+
            '<div class="v90-overview-head"><div class="v90-overview-asset">'+label(a)+'</div>'+
            '<div class="v90-overview-dir '+cls(consensus)+'">'+arrow(consensus)+' '+consensus+'</div>'+
            '<div class="v90-overview-regime">'+regime+'</div><div class="v90-overview-price">'+fmt(price)+'</div></div>'+
            '<div class="v90-overview-tfs">'+tfhtml+'</div>'+
            '<div class="v90-overview-foot"><div>подтверждения <b>'+indep+'</b></div><div>лучший R/R <b>'+(bestRR?bestRR.toFixed(2):'—')+'</b></div><div>исполнение <b>'+execN+'/'+xs.length+'</b></div></div>'+
          '</div>';
        }).join('');
        el.innerHTML='<div class="v90-overview-grid">'+html+'</div>';
      };
    })();
    </script>"""
    value=value.replace('</body>',dense_market_js+'</body>')
    fast_signal_js = r"""<script id="V90_FAST_SIGNAL_FEED">
    (function(){
      let v90SignalsBusy=false;
      async function v90LoadSignals(){
        if(v90SignalsBusy)return;
        v90SignalsBusy=true;
        try{
          const ctl=new AbortController();
          const tm=setTimeout(()=>ctl.abort(),6000);
          const r=await fetch('/api/v1/signals',{cache:'no-store',signal:ctl.signal});
          clearTimeout(tm);
          if(!r.ok)throw new Error('signals HTTP '+r.status);
          const d=await r.json();
          const rows=Array.isArray(d.signals)?d.signals:[];
          if(typeof renderMatrix==='function')renderMatrix(rows);
          window.V90_LAST_SIGNALS=rows;
          if(typeof renderDenseAssetOverview==='function')renderDenseAssetOverview(rows);
          if(window.V90_SELECTED_SIGNAL && typeof window.showDetail==='function'){
            window.showDetail(window.V90_SELECTED_SIGNAL.asset,window.V90_SELECTED_SIGNAL.horizon,true);
          }
          const stamp=document.getElementById('stamp');
          if(stamp){
            const at=d.at?new Date(d.at):new Date();
            stamp.textContent='сигналы '+at.toLocaleString()+' · '+rows.length+'/42';
          }
        }catch(e){
          const ms=document.getElementById('matrixstatus');
          if(ms && !ms.textContent.trim())ms.textContent='обновление сигналов…';
        }finally{
          v90SignalsBusy=false;
        }
      }
      window.v90LoadSignals=v90LoadSignals;
      if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded',()=>setTimeout(v90LoadSignals,0),{once:true});
      }else{
        setTimeout(v90LoadSignals,0);
      }
      setInterval(v90LoadSignals,20000);
    })();
    </script>"""
    value = value.replace('</body>', fast_signal_js + '</body>')
    signal_detail_sync_js = r"""<script id="V90_SIGNAL_DETAIL_SYNC">
    (function(){
      window.v90MetricLabel=function(value,source){
        if(value==null)return '—';
        const x=(100*Number(value)).toFixed(1)+'%';
        if(source==='EMPIRICAL_CALIBRATION'||source==='CALIBRATED_PROBABILITY')return x+' · calibr.';
        if(String(source||'').includes('MODEL_QUALITY_SCORE'))return x+' · model score';
        return x+' · model';
      };

      window.showDetail=async function(asset,horizon,autoRefresh){
        window.V90_SELECTED_SIGNAL={asset:asset,horizon:horizon};
        const el=document.getElementById('detail');
        if(!autoRefresh)el.textContent='загрузка…';
        try{
          const r=await fetch('/api/v1/explain?asset='+encodeURIComponent(asset)+'&horizon='+encodeURIComponent(horizon),{cache:'no-store'});
          const d=(await r.json()).explanation||{};
          if(d.status!=='ok'){el.textContent='нет данных';return}
          const cp=(d.calibration||{}).probability_correct;
          const fmt=a=>(a||[]).map(x=>'<div>'+x.agent+': '+(x.direction||'')+'</div>').join('')||'—';
          const ex=d.execution_eligibility||{},ti=d.trend_impulse||{},st=d.intraday_structure||{},tp=d.trade_plan||{};
          const sg=d.structure_breakout_grid||{};
          const structureLine=['5m','1h','4h','1d','3d','7d'].map(tf=>{
            const z=sg[tf]||{};
            if(z.status!=='OK')return tf+' —';
            const dir=z.direction||'—',state=z.state||'WAIT';
            const vx=z.volatility_expansion_ratio==null?'':(' · vol×'+Number(z.volatility_expansion_ratio).toFixed(2));
            const flag=z.entry_signal?' · ENTRY':z.exit_signal?' · EXIT':'';
            return tf+' '+dir+' '+state+vx+flag;
          }).join(' · ');
          const mtf=tp.multi_tf_levels||{},ctx=tp.multi_tf_level_context||{};
          const tfrows=(mtf.timeframes||{});
          const levelLine=['5m','1h','4h','1d','3d','7d'].map(tf=>{
            const z=tfrows[tf]||{};
            if(z.status!=='OK')return tf+' —';
            const s=z.support==null?'—':Number(z.support).toFixed(3);
            const rr=z.resistance==null?'—':Number(z.resistance).toFixed(3);
            return tf+' S '+s+' / R '+rr;
          }).join(' · ');
          let gate='';
          if(ex.production_eligible===false && ex.paper_eligible){
            gate='<b class="warn">PAPER</b> · 1 прямой источник · production НЕТ';
          }else{
            gate='<b>'+(ex.eligible?'ДА':'НЕТ')+'</b>'+(ex.reason?' · '+ex.reason:'');
          }
          const rawOn=ti.raw_onset_score==null?ti.onset_score:ti.raw_onset_score;
          const rawImp=ti.raw_impulse_score==null?ti.impulse_score:ti.raw_impulse_score;
          const sc=ti.structural_confirmation_score==null?st.score:ti.structural_confirmation_score;
          const stamp=d.event_ts?new Date(d.event_ts).toLocaleString('ru-RU'):'—';
          el.innerHTML=
            '<b>'+d.asset+' · '+d.horizon+'</b> · '+tierText({decision:d.decision,research_decision:d.research_decision,signal_tier:d.signal_tier})+
            '<br><span class="stamp">снимок '+stamp+'</span>'+
            '<br>Сила: '+pct(d.confidence)+' · калиброванная вероятность: '+(cp==null?'ещё недостаточно данных':pct(cp))+' · режим: '+(d.regime||'—')+
            '<br>Тренд: <b>'+(ti.phase||'NONE')+'</b> · onset '+pct(rawOn)+' · impulse '+pct(rawImp)+' · структура '+pct(sc)+' · вход '+(ti.entry_quality||'—')+
            '<br>Структура: '+(st.lifecycle||'—')+' · score '+pct(st.score)+' · удержание пробоя '+(st.breakout_hold?'ДА':'НЕТ')+' · rVol '+(st.relative_volume==null?'—':Number(st.relative_volume).toFixed(2))+
            '<br>План: тех. потенциал '+(tp.expected_move_pct==null?'—':pct(tp.expected_move_pct))+
              ' · цель '+(tp.target_price==null?'—':Number(tp.target_price).toFixed(3))+
              ' · стоп '+(tp.stop_price==null?'—':Number(tp.stop_price).toFixed(3))+
              ' · R/R '+(tp.expected_to_stop_ratio==null?'—':Number(tp.expected_to_stop_ratio).toFixed(2))+
            '<br><b>Структурный контур:</b> '+(structureLine||'—')+
            '<br><b>Уровни по ТФ:</b> '+(levelLine||'—')+
            '<br><span class="stamp">для '+d.horizon+' учитываются: '+((ctx.considered_timeframes||[]).join(' → ')||'—')+
              '; старшие уровни ограничивают цель/инвалидацию, младший ТФ используется для тайминга входа</span>'+
            '<br>Decision Edge: <b>'+(d.decision_stage||tp.decision_stage||'—')+'</b> · P+ '+
              (d.positive_trade_probability==null?(tp.positive_trade_probability==null?'накапливается':pct(tp.positive_trade_probability)):pct(d.positive_trade_probability))+
              ' · аналоги n≈'+(d.analog_effective_n??(tp.tradeability||{}).effective_n??'—')+
            '<br>Торговый допуск: '+gate+
            '<div class="detail-grid"><div class="detail-col"><div class="detail-title">За</div>'+fmt(d.pro)+'</div>'+
            '<div class="detail-col"><div class="detail-title">Против</div>'+fmt(d.con)+'</div>'+
            '<div class="detail-col"><div class="detail-title">Риск</div>'+fmt(d.risk)+'</div></div>'+
            '<div style="margin-top:8px">Совпало правил знаний: '+((d.knowledge_matches||[]).length)+'</div>';
        }catch(e){
          if(!autoRefresh)el.textContent=String(e);
        }
      };
    })();
    </script>"""
    value = value.replace('</body>', signal_detail_sync_js + '</body>')
    closed_journal_v2_css = r"""<style id="V90_CLOSED_JOURNAL_V2_STYLE">
    .v90-closed-wrap{display:grid;gap:14px}
    .v90-closed-topline{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:9px 11px;border:1px solid rgba(130,145,160,.18);border-radius:10px}
    .v90-closed-topline .metric{font-size:12px;opacity:.9}
    .v90-closed-section-title{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin:4px 0 2px}
    .v90-today-group{display:grid;gap:7px}
    .v90-today-head{display:flex;flex-wrap:wrap;gap:7px 12px;align-items:center;font-size:12px;padding:6px 2px}
    .v90-today-card{padding:10px 11px!important}
    .v90-today-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px 12px;margin-top:7px}
    .v90-today-grid>div{font-size:11px;line-height:1.35;min-width:0}
    .v90-today-grid .wide{grid-column:span 2}
    .v90-today-learning{margin-top:7px;padding-top:6px;border-top:1px solid rgba(130,145,160,.16);font-size:11px;line-height:1.4}
    .v90-telemetry-warn{margin-top:6px;font-size:10px;opacity:.8}
    .v90-archive-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
    .v90-archive-card{padding:9px 10px;border:1px solid rgba(130,145,160,.16);border-radius:9px;font-size:11px;line-height:1.5}
    .v90-memory-list{display:grid;gap:6px}
    .v90-memory-row{padding:8px 10px;border:1px solid rgba(130,145,160,.14);border-radius:8px;font-size:11px;line-height:1.4}
    .v90-memory-head{display:flex;justify-content:space-between;gap:10px;align-items:center}
    .v90-memory-meta{opacity:.78;margin-top:3px}
    .v90-missing{padding:7px 9px;border:1px solid rgba(180,150,90,.25);border-radius:8px;font-size:10px}
    @media(max-width:900px){.v90-today-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.v90-archive-grid{grid-template-columns:1fr}}
    @media(max-width:560px){.v90-today-grid{grid-template-columns:1fr}.v90-today-grid .wide{grid-column:span 1}}
    </style>"""
    value = value.replace('</head>', closed_journal_v2_css + '</head>')
    closed_journal_v2_js = r"""<script id="V90_CLOSED_JOURNAL_V2">
    (function(){
      const ORDER=['Impulse','Aggressive','Champion','Challenger'];
      const LABELS={RIGHT_DIRECTION_HIGH_CAPTURE:'Высокий захват движения',RIGHT_DIRECTION_LOW_CAPTURE:'Направление верное, захват движения низкий',RIGHT_DIRECTION_STOP_ERROR:'Верное направление, ошибка стопа',FAVORABLE_PATH_NOT_MONETIZED:'Благоприятный ход не монетизирован',RIGHT_DIRECTION_PREMATURE_EXIT:'Преждевременный выход',DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH:'Ошибка направления или входа',GOOD_EXECUTION:'Хорошее исполнение',MIXED_EXECUTION:'Смешанное исполнение',RECOVERED_HISTORICAL_NO_LEARNING:'История восстановлена частично'};
      const FIELDS={closed_at:'время закрытия',exit_reason:'причина выхода',quantity:'количество',stop_price:'SL',take_price:'TP',mfe_pct:'MFE',mae_pct:'MAE',regime:'режим',learning_label:'вывод обучения'};
      const rubv=v=>v==null?'—':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽';
      const px=v=>v==null?'—':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:4});
      const pct=v=>v==null?'—':Number(v).toFixed(2)+'%';
      const tm=v=>v?new Date(v).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
      const hold=v=>v==null?'—':Number(v)>=86400?(Number(v)/86400).toFixed(1)+' д':Number(v)>=3600?(Number(v)/3600).toFixed(1)+' ч':Math.max(1,Math.round(Number(v)/60))+' мин';
      const metric=(v,src)=>v==null?'—':(typeof window.v90MetricLabel==='function'?window.v90MetricLabel(v,src):(100*Number(v)).toFixed(1)+'%');
      const esc=s=>String(s??'—').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
      function todayCard(t){
        const pnl=Number(t.net_pnl_rub||0), complete=100*Number(t.telemetry_completeness||0), label=LABELS[t.learning_label]||t.learning_label||'—';
        return `<div class="assetview v90-today-card"><div class="closed-trade-head"><b>${esc(t.asset)} · ${esc(t.direction)} · ${esc(t.horizon)}</b><b class="${pnl>=0?'ok':'bad'}">${rubv(t.net_pnl_rub)}${t.return_pct==null?'':' · '+Number(t.return_pct).toFixed(2)+'%'}</b></div><div class="v90-today-grid"><div>Вход <b>${px(t.avg_entry_price)}</b></div><div>Выход <b>${px(t.avg_exit_price)}</b></div><div>Открыта <b>${tm(t.opened_at)}</b></div><div>Закрыта <b>${tm(t.closed_at)}</b></div><div>Удержание <b>${hold(t.held_seconds)}</b></div><div>Количество <b>${t.quantity==null?'—':Number(t.quantity).toLocaleString('ru-RU',{maximumFractionDigits:5})}</b></div><div>Gross <b>${rubv(t.gross_pnl_rub)}</b></div><div>Net <b class="${pnl>=0?'ok':'bad'}">${rubv(t.net_pnl_rub)}</b></div><div>Комиссия <b>${rubv(t.fees_rub||0)}</b></div><div>Фондирование <b>${rubv(t.funding_rub||0)}</b></div><div>SL <b>${px(t.stop_price)}</b></div><div>TP <b>${px(t.take_price)}</b></div><div>MFE <b>${pct(t.mfe_pct)}</b></div><div>MAE <b>${pct(t.mae_pct)}</b></div><div>Giveback <b>${pct(t.giveback_pct)}</b></div><div>Причина выхода <b>${esc(t.exit_reason)}</b></div><div class="wide">Setup <b>${esc(t.setup||t.setup_family)}</b> · режим <b>${esc(t.regime)}</b></div><div class="wide">Входной score / Prob-ty <b title="${esc(t.probability_source)}">${metric(t.entry_probability,t.probability_source)}</b></div></div><div class="v90-today-learning"><b>Вывод для обучения:</b> ${esc(label)} · ${esc(t.learning_conclusion)}</div><div class="v90-telemetry-warn">Полнота телеметрии ${complete.toFixed(0)}%${t.recovered?'<span class="warn"> · восстановленная запись</span>':''}</div></div>`;
      }
      function todayGroup(name,rows){
        const wins=rows.filter(x=>Number(x.net_pnl_rub||0)>0).length,net=rows.reduce((s,x)=>s+Number(x.net_pnl_rub||0),0),wr=rows.length?100*wins/rows.length:0;
        return `<div class="v90-today-group"><div class="v90-today-head"><b>${esc(name)}</b><span>${rows.length} закрыто</span><span>${wins} прибыльных</span><span>win ${wr.toFixed(1)}%</span><b class="${net>=0?'ok':'bad'}">${rubv(net)}</b></div>${rows.map(todayCard).join('')}</div>`;
      }
      function archiveCard(x){
        const n=Number(x.closed_trades||0),wins=Number(x.wins||0),wr=x.win_rate==null?0:100*Number(x.win_rate),cost=Number(x.fees_rub||0)+Number(x.funding_rub||0);
        return `<div class="v90-archive-card"><b>${esc(x.portfolio_name)}</b><br>Закрыто <b>${n}</b> · прибыльных <b>${wins}</b> · win <b>${wr.toFixed(1)}%</b><br>Gross <b>${rubv(x.gross_pnl_rub)}</b> · расходы <b>${rubv(cost)}</b><br>Итог <b class="${Number(x.net_pnl_rub||0)>=0?'ok':'bad'}">${rubv(x.net_pnl_rub)}</b></div>`;
      }
      function memoryRow(x){
        const label=LABELS[x.learning_label]||x.learning_label||'—',ret=x.avg_return_pct==null?'—':Number(x.avg_return_pct).toFixed(2)+'%',weight=x.learning_weight==null?'—':Number(x.learning_weight).toFixed(2);
        return `<div class="v90-memory-row"><div class="v90-memory-head"><b>${esc(x.asset)} · ${esc(x.direction)} · ${esc(x.horizon)}</b><b class="${Number(x.total_net_pnl_rub||0)>=0?'ok':'bad'}">${rubv(x.total_net_pnl_rub)}</b></div><div class="v90-memory-meta">${esc(x.setup_family||x.setup)} · ${esc(x.regime)} · ${x.portfolio_count||0} портф. / ${x.trade_count||0} исполн. · avg ${ret}</div><div><b>${esc(label)}</b> · ${esc(x.learning_conclusion)}</div><div class="stamp">MFE ${pct(x.avg_mfe_pct)} · MAE ${pct(x.avg_mae_pct)} · giveback ${pct(x.avg_giveback_pct)} · вес обучения ${weight} · ${x.learning_eligible?'учитывается':'только архив'}</div></div>`;
      }
      function render(d){
        const today=Array.isArray(d.today_trades)?d.today_trades:(Array.isArray(d.trades)?d.trades:[]),history=Array.isArray(d.history_summary)?d.history_summary:[],q2=Array.isArray(d.quality_r2_summary)?d.quality_r2_summary:[],memory=Array.isArray(d.older_unique_learning)?d.older_unique_learning:[];
        const groups={};today.forEach(t=>(groups[t.portfolio_name||'—']||(groups[t.portfolio_name||'—']=[])).push(t));
        const names=Object.keys(groups).sort((a,b)=>{const ia=ORDER.indexOf(a),ib=ORDER.indexOf(b);return (ia<0?999:ia)-(ib<0?999:ib)||a.localeCompare(b)});
        const missing=Object.entries(d.today_missing_fields||{}).filter(([k,v])=>Number(v)>0),recovery=d.today_recovery||{};
        const warn=missing.length?`<div class="v90-missing"><b>Неполные поля в сегодняшних сделках:</b> ${missing.map(([k,v])=>esc(FIELDS[k]||k)+' '+v).join(' · ')}. Неполные исторические эпизоды не усиливают обучение.</div>`:'';
        const top=`<div class="v90-closed-topline"><span class="metric">Сегодня <b>${d.today_closed_count??today.length}</b></span><span class="metric">Архив <b>${d.older_closed_count??0}</b></span><span class="metric">Уникальных эпизодов <b>${d.unique_learning_count??0}</b></span><span class="metric">Допущено к обучению <b>${d.learning_eligible_count??0}</b></span><span class="metric">Дубликатов портфелей объединено <b>${d.deduplicated_portfolio_records??0}</b></span><span class="metric">Восстановлено path <b>${recovery.shadow_path_recovered??0}</b></span></div>`;
        const todayHtml='<div class="v90-closed-section-title">Сегодня · подробно по портфелям</div>'+(names.length?names.map(n=>todayGroup(n,groups[n])).join(''):'<div class="stamp">Сегодня закрытых сделок пока нет.</div>');
        const q2Html='<div class="v90-closed-section-title">После Quality Gate R2 · новая логика</div>'+(q2.length?`<div class="v90-archive-grid">${q2.map(archiveCard).join('')}</div>`:'<div class="stamp">После R2 закрытых сделок пока недостаточно. Старая статистика не смешивается с новой.</div>');\n        const histHtml='<div class="v90-closed-section-title">Вся история · аудит</div>'+(history.length?`<div class="v90-archive-grid">${history.map(archiveCard).join('')}</div>`:'<div class="stamp">Исторических закрытых сделок пока нет.</div>');
        const memHtml='<div class="v90-closed-section-title">Уникальная память для самообучения</div>'+(memory.length?`<div class="v90-memory-list">${memory.slice(0,50).map(memoryRow).join('')}</div>`:'<div class="stamp">Уникальные исторические эпизоды накапливаются.</div>');
        return `<div class="v90-closed-wrap">${top}${warn}${todayHtml}${q2Html}${histHtml}${memHtml}</div>`;
      }
      let refreshBusy=false;
      async function refresh(){
        const el=document.getElementById('portfoliotrades');if(!el||refreshBusy)return;
        refreshBusy=true;
        try{
          const ctl=new AbortController();const tm=setTimeout(()=>ctl.abort(),12000);
          const r=await fetch('/api/v1/portfolio-trades',{cache:'no-store',signal:ctl.signal});clearTimeout(tm);
          if(!r.ok)throw new Error('HTTP '+r.status);
          const d=await r.json();
          el.dataset.v90Managed='1';
          el.innerHTML=render(d);
          el.dataset.v90JournalState='ok';
        }catch(e){
          el.dataset.v90JournalState='error';
          if(!el.innerHTML.trim()||el.textContent.includes('загружается'))el.innerHTML='<span class="warn">Журнал закрытых сделок временно обновляется…</span>';
        }finally{refreshBusy=false}
      }
      function start(){
        const el=document.getElementById('portfoliotrades');if(!el)return;
        el.dataset.v90Managed='1';
        setTimeout(refresh,8000);
        setInterval(refresh,120000);
      }
      if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
    })();
    </script>"""
    value = value.replace('</body>', closed_journal_v2_js + '</body>')

    # VERITAS V90 DECISION COCKPIT R15
    cockpit_css = """<style id="V90_DECISION_COCKPIT_R15">
    #v90-cockpit{grid-column:span 12;margin:2px 0 12px;display:grid;gap:10px}
    .v90-cp-hero{display:grid;grid-template-columns:1.55fr .85fr;gap:10px}
    .v90-cp-card{border:1px solid var(--border);border-radius:14px;background:rgba(18,23,28,.86);padding:12px 14px;min-width:0}
    .v90-cp-title{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
    .v90-cp-opps{display:grid;gap:6px}
    .v90-cp-opp{display:grid;grid-template-columns:72px 58px 56px minmax(0,1fr) 86px 86px;gap:7px;align-items:center;padding:7px 8px;border:1px solid var(--border);border-radius:10px}
    .v90-cp-opp b{font-size:11px}.v90-cp-opp span{font-size:8px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .v90-cp-long{color:#59d694}.v90-cp-short{color:#ef6767}.v90-cp-wait{color:#d5b65b}
    .v90-cp-kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
    .v90-cp-kpi{padding:9px 10px;border:1px solid var(--border);border-radius:10px;min-width:0}
    .v90-cp-kpi span{display:block;color:var(--muted);font-size:8px;text-transform:uppercase;letter-spacing:.06em}
    .v90-cp-kpi b{display:block;font-size:17px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .v90-cp-kpi small{display:block;font-size:7.5px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .v90-cp-portfolios{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px}
    .v90-cp-pf{padding:9px 10px;border:1px solid var(--border);border-radius:10px;min-width:0}
    .v90-cp-pf-head{display:flex;justify-content:space-between;gap:6px;align-items:baseline}
    .v90-cp-pf-head b{font-size:10px}.v90-cp-pf-head span{font-size:9px}
    .v90-cp-pf-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 7px;margin-top:6px;font-size:8px;color:var(--muted)}
    .v90-cp-pf-grid b{color:var(--text);font-size:8.5px}
    .v90-cp-status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px}
    .v90-cp-status>div{padding:8px 10px;border:1px solid var(--border);border-radius:10px}
    .v90-cp-status span{font-size:7.5px;color:var(--muted);text-transform:uppercase}.v90-cp-status b{display:block;font-size:10px;margin-top:2px}
    .v90-cp-note{font-size:8px;color:var(--muted);line-height:1.35}
    @media(max-width:900px){
      .v90-cp-hero{grid-template-columns:1fr}
      .v90-cp-opp{grid-template-columns:58px 50px 46px minmax(0,1fr);gap:5px}
      .v90-cp-opp .v90-cp-stop,.v90-cp-opp .v90-cp-target{display:none}
      .v90-cp-portfolios{grid-template-columns:repeat(2,minmax(0,1fr))}
      .v90-cp-status{grid-template-columns:repeat(2,minmax(0,1fr))}
    }
    </style>"""
    value=value.replace('</head>',cockpit_css+'</head>')

    cockpit_js = r"""<script id="V90_DECISION_COCKPIT_R15">
    (function(){
      const esc=s=>String(s==null?'—':s).replace(/[&<>"']/g,function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]});
      const px=v=>v==null?'—':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:4});
      const rub=v=>v==null?'—':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽';
      const pct=v=>v==null?'—':Number(v).toFixed(2)+'%';
      const dirClass=d=>d==='LONG'?'v90-cp-long':d==='SHORT'?'v90-cp-short':'v90-cp-wait';
      function install(){
        const market=document.getElementById('market'); if(!market||document.getElementById('v90-cockpit'))return;
        const el=document.createElement('section');el.id='v90-cockpit';
        el.innerHTML='<div class="v90-cp-hero">'
          +'<div class="v90-cp-card"><div class="v90-cp-title">Что делать сейчас</div><div id="v90-cp-opps" class="v90-cp-opps"><div class="v90-cp-note">Собираю лучшие возможности…</div></div></div>'
          +'<div class="v90-cp-card"><div class="v90-cp-title">Интеллект VERITAS</div><div class="v90-cp-kpis">'
          +'<div class="v90-cp-kpi"><span>Intelligence Index</span><b id="v90-cp-ii">—</b><small id="v90-cp-iic">ожидание выборки</small></div>'
          +'<div class="v90-cp-kpi"><span>Чистых эпизодов</span><b id="v90-cp-episodes">—</b><small>после Quality Gate R2</small></div>'
          +'<div class="v90-cp-kpi"><span>Win-rate</span><b id="v90-cp-wr">—</b><small>только новая логика</small></div>'
          +'<div class="v90-cp-kpi"><span>Capture ratio</span><b id="v90-cp-cap">—</b><small>сколько движения забираем</small></div>'
          +'</div></div></div>'
          +'<div class="v90-cp-card"><div class="v90-cp-title">Портфели сейчас</div><div id="v90-cp-portfolios" class="v90-cp-portfolios"><div class="v90-cp-note">Загрузка портфелей…</div></div></div>'
          +'<div class="v90-cp-card"><div class="v90-cp-title">Состояние системы</div><div class="v90-cp-status">'
          +'<div><span>System</span><b id="v90-cp-system">—</b></div><div><span>Database</span><b id="v90-cp-db">—</b></div><div><span>Market data</span><b id="v90-cp-data">—</b></div><div><span>Market</span><b id="v90-cp-market">—</b></div>'
          +'</div><div class="v90-cp-note" style="margin-top:7px">Закрытый рынок или исследовательский источник не означает сбой всей системы.</div></div>';
        market.insertBefore(el,market.firstChild);
      }
      function renderSignals(d){
        const rows=Array.isArray(d&&d.signals)?d.signals:[];
        const rank=function(x){
          const dec=String(x.research_decision||x.decision||'NO_TRADE');
          if(['LONG','SHORT'].indexOf(dec)<0)return -999;
          return 4*Number(x.horizon_structure_score||0)+2*Number(x.confidence||0)+Math.min(Number(x.expected_to_stop_ratio||0),3)+0.3*Number(x.independent_evidence_families||0)+(x.entry_quality==='FRESH_BREAKOUT'?1:0);
        };
        const best=rows.filter(function(x){return ['LONG','SHORT'].indexOf(String(x.research_decision||x.decision||''))>=0}).sort(function(a,b){return rank(b)-rank(a)}).slice(0,5);
        const el=document.getElementById('v90-cp-opps');if(!el)return;
        if(!best.length){el.innerHTML='<div class="v90-cp-note">Сейчас нет подтверждённых входов. NO TRADE лучше слабой сделки.</div>';return}
        el.innerHTML=best.map(function(x){
          const d=String(x.research_decision||x.decision||'NO_TRADE'), reason=x.plan_reason||x.entry_quality||x.regime||'—';
          return '<div class="v90-cp-opp"><b>'+esc(x.asset)+'</b><b class="'+dirClass(d)+'">'+esc(d)+'</b><span>'+esc(x.horizon)+'</span><span>'+esc(reason)+' · HS '+Number(x.horizon_structure_score||0).toFixed(2)+' · RR '+(x.expected_to_stop_ratio==null?'—':Number(x.expected_to_stop_ratio).toFixed(2))+'</span><span class="v90-cp-stop">SL '+px(x.stop_price)+'</span><span class="v90-cp-target">TP '+px(x.target_price)+'</span></div>';
        }).join('');
      }
      function renderPortfolios(d){
        const ps=Array.isArray(d&&d.portfolios)?d.portfolios:[], el=document.getElementById('v90-cp-portfolios');if(!el)return;
        if(!ps.length){el.innerHTML='<div class="v90-cp-note">Портфели временно недоступны.</div>';return}
        el.innerHTML=ps.map(function(p){
          const l=p.latest||{}, ret=l.total_return_pct!=null?l.total_return_pct:p.total_return_pct, gross=l.gross_leverage||0, nav=l.nav_rub!=null?l.nav_rub:p.nav_rub;
          return '<div class="v90-cp-pf"><div class="v90-cp-pf-head"><b>'+esc(p.name)+'</b><span class="'+(Number(ret||0)>=0?'ok':'bad')+'">'+pct(ret)+'</span></div><div class="v90-cp-pf-grid"><span>NAV</span><b>'+rub(nav)+'</b><span>Gross</span><b>'+Number(gross||0).toFixed(2)+'×</b><span>Позиций</span><b>'+(Array.isArray(p.positions)?p.positions.length:0)+'</b><span>Win</span><b>'+(p.win_rate==null?'—':pct(100*Number(p.win_rate)))+'</b></div></div>';
        }).join('');
        const ii=d.intelligence_index||{}, ev=ii.evidence||{};
        const set=function(id,v){const x=document.getElementById(id);if(x)x.textContent=v};
        set('v90-cp-ii',ii.score==null?'—':Number(ii.score).toFixed(1)+'/100');
        set('v90-cp-iic',ii.confidence?('confidence '+ii.confidence):'ожидание выборки');
        set('v90-cp-episodes',ev.clean_post_r2_closed_trades==null?'—':ev.clean_post_r2_closed_trades);
        set('v90-cp-wr',ev.win_rate==null?'—':(100*Number(ev.win_rate)).toFixed(1)+'%');
        set('v90-cp-cap',ev.avg_capture_ratio==null?'—':(100*Number(ev.avg_capture_ratio)).toFixed(0)+'%');
      }
      function renderHealth(h,s){
        const set=function(id,v,cls){const x=document.getElementById(id);if(x){x.textContent=v;x.className=cls||''}};
        set('v90-cp-system',h&&h.ok?'OK':'STARTING',h&&h.ok?'ok':'warn');
        set('v90-cp-db',h&&h.bootstrap_ready?'OK':'INIT',h&&h.bootstrap_ready?'ok':'warn');
        const rows=Array.isArray(s&&s.signals)?s.signals:[], live=rows.filter(function(x){return x.source_gate_pass===true}).length, total=rows.length;
        set('v90-cp-data',total?(live+'/'+total+' LIVE'):'—',live?'ok':'warn');
        const open=rows.some(function(x){return x.market_open===true});
        set('v90-cp-market',open?'OPEN / MIXED':'CLOSED / RESEARCH',open?'ok':'warn');
      }
      async function v90FetchJson(url,timeoutMs){
        const ctl=new AbortController();const tm=setTimeout(()=>ctl.abort(),timeoutMs||5000);
        try{
          const r=await fetch(url,{cache:'no-store',signal:ctl.signal});
          if(!r.ok)throw new Error('HTTP '+r.status);
          return await r.json();
        }finally{clearTimeout(tm)}
      }
      async function refresh(){
        install();
        // Critical path: signals + health only. Never wait for PostgreSQL-heavy portfolio APIs.
        const fast=await Promise.allSettled([
          v90FetchJson('/api/v1/signals',5000),
          v90FetchJson('/healthz',3000)
        ]);
        const s=fast[0].status==='fulfilled'?fast[0].value:null;
        const h=fast[1].status==='fulfilled'?fast[1].value:null;
        if(s)renderSignals(s);
        renderHealth(h,s);

        // Portfolio state is secondary and fail-soft.
        v90FetchJson('/api/v1/paper-portfolios',6000)
          .then(renderPortfolios)
          .catch(function(){});
      }
      if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',refresh,{once:true});else refresh();
      setInterval(refresh,30000);
    })();
    </script>"""
    value=value.replace('</body>',cockpit_js+'</body>')

    # VERITAS V90 EXECUTIVE PANEL R21
    exec_css = """<style id="V90_EXECUTIVE_PANEL_R21">
    #v90-cockpit{gap:7px!important;margin-bottom:8px!important}
    #v90-r21{display:grid;grid-template-columns:1.45fr 1.1fr .9fr;gap:7px}
    .r21-box{border:1px solid var(--border);border-radius:11px;background:rgba(18,23,28,.9);padding:9px 10px;min-width:0}
    .r21-title{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}
    .r21-action{display:grid;grid-template-columns:62px 52px 42px 1fr 70px 70px;gap:5px;align-items:center;padding:5px 6px;border-top:1px solid rgba(255,255,255,.05)}
    .r21-action:first-child{border-top:0}.r21-action b{font-size:10px}.r21-action span{font-size:7.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .r21-map{display:grid;gap:3px}
    .r21-asset{display:grid;grid-template-columns:58px 45px 1fr 54px;gap:5px;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)}
    .r21-asset:last-child{border-bottom:0}.r21-asset b{font-size:9px}.r21-tfs{font-size:7px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .r21-price{font-size:8px;text-align:right}
    .r21-kpis{display:grid;grid-template-columns:1fr 1fr;gap:5px}
    .r21-kpi{border:1px solid var(--border);border-radius:8px;padding:6px 7px;min-width:0}
    .r21-kpi span{display:block;font-size:7px;color:var(--muted);text-transform:uppercase}.r21-kpi b{display:block;font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .r21-pf{display:grid;grid-template-columns:72px 1fr 48px 48px;gap:5px;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:8px}
    .r21-pf:last-child{border-bottom:0}.r21-pf b{font-size:8.5px}
    .r21-status{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}.r21-pill{padding:3px 6px;border:1px solid var(--border);border-radius:999px;font-size:7px;color:var(--muted)}
    #market>.card[data-r21-hidden="1"]{display:none!important}
    @media(max-width:1000px){#v90-r21{grid-template-columns:1fr}.r21-action{grid-template-columns:56px 48px 40px 1fr}.r21-action .r21-sl,.r21-action .r21-tp{display:none}}
    </style>"""
    value=value.replace('</head>',exec_css+'</head>')

    exec_js = r"""<script id="V90_EXECUTIVE_PANEL_R21">
    (function(){
      const TF=['5m','1h','4h','1d','3d','7d'];
      const AS=['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'];
      const lab=a=>a==='NQ'?'NDXf':a==='CNYRUBF'?'CNYRUBf':a;
      const esc=s=>String(s==null?'—':s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
      const dir=x=>String((x&&x.research_decision)||x?.decision||'NO_TRADE');
      const dc=d=>d==='LONG'?'ok':d==='SHORT'?'bad':'warn';
      const ar=d=>d==='LONG'?'↑':d==='SHORT'?'↓':'→';
      const px=v=>{const n=Number(v);return Number.isFinite(n)?n.toLocaleString('en-US',{maximumFractionDigits:n>=1000?2:4}):'—'};
      let prev={};

      function hideNoise(){
        const keep=new Set(['Сигналы по инструментам','Разбор выбранного сигнала','Макро / кросс-активы']);
        document.querySelectorAll('#market>.card').forEach(card=>{
          const k=card.querySelector('.k');
          if(!k)return;
          const t=(k.textContent||'').trim();
          if(!keep.has(t))card.dataset.r21Hidden='1';
        });
      }

      function install(){
        const cp=document.getElementById('v90-cockpit');if(!cp)return;
        cp.innerHTML='<div id="v90-r21">'
          +'<div class="r21-box"><div class="r21-title">Что делать сейчас</div><div id="r21-actions">—</div></div>'
          +'<div class="r21-box"><div class="r21-title">Рынок · 6 таймфреймов</div><div id="r21-map" class="r21-map">—</div></div>'
          +'<div class="r21-box"><div class="r21-title">Портфель и риск</div><div id="r21-pf">—</div><div class="r21-kpis" style="margin-top:6px">'
          +'<div class="r21-kpi"><span>Лучшая доходность</span><b id="r21-bestret">—</b></div>'
          +'<div class="r21-kpi"><span>Макс. просадка</span><b id="r21-maxdd">—</b></div>'
          +'<div class="r21-kpi"><span>Открытых позиций</span><b id="r21-open">—</b></div>'
          +'<div class="r21-kpi"><span>Состояние данных</span><b id="r21-data">—</b></div>'
          +'</div><div id="r21-status" class="r21-status"></div></div>'
          +'</div>';
        hideNoise();
      }

      function renderSignals(d){
        const rows=Array.isArray(d?.signals)?d.signals:[];
        const map={};rows.forEach(x=>{if(x?.asset&&x?.horizon)map[x.asset+'|'+x.horizon]=x});
        const rank=x=>{
          const D=dir(x); if(!['LONG','SHORT'].includes(D))return -999;
          const eligible=x.plan_eligible!==false;
          return (eligible?2:0)+4*Number(x.horizon_structure_score||0)+2*Number(x.confidence||0)+Math.min(Number(x.expected_to_stop_ratio||0),3)+.25*Number(x.independent_evidence_families||0);
        };
        const best=rows.filter(x=>['LONG','SHORT'].includes(dir(x))).sort((a,b)=>rank(b)-rank(a)).slice(0,4);
        document.getElementById('r21-actions').innerHTML=best.length?best.map(x=>{
          const D=dir(x), reason=x.plan_reason||x.entry_quality||x.regime||'—';
          return '<div class="r21-action"><b>'+lab(x.asset)+'</b><b class="'+dc(D)+'">'+ar(D)+' '+D+'</b><span>'+x.horizon+'</span><span>'+esc(reason)+' · R/R '+(x.expected_to_stop_ratio==null?'—':Number(x.expected_to_stop_ratio).toFixed(2))+'</span><span class="r21-sl">SL '+px(x.stop_price)+'</span><span class="r21-tp">TP '+px(x.target_price)+'</span></div>';
        }).join(''):'<span class="stamp">Сильных входов нет — ждать.</span>';

        document.getElementById('r21-map').innerHTML=AS.map(a=>{
          const xs=TF.map(tf=>map[a+'|'+tf]).filter(Boolean);
          const dirs=xs.map(dir), ln=dirs.filter(d=>d==='LONG').length, sn=dirs.filter(d=>d==='SHORT').length;
          const D=ln>sn?'LONG':sn>ln?'SHORT':'WAIT';
          const p=(map[a+'|5m']||xs[0]||{}).price;
          const tf=TF.map(t=>{const x=map[a+'|'+t];return t+':' +(x?ar(dir(x)):'—')}).join(' ');
          return '<div class="r21-asset"><b>'+lab(a)+'</b><b class="'+dc(D)+'">'+ar(D)+' '+D+'</b><span class="r21-tfs">'+tf+'</span><span class="r21-price">'+px(p)+'</span></div>';
        }).join('');

        const changed=[];
        rows.forEach(x=>{
          const k=x.asset+'|'+x.horizon, D=dir(x);
          if(prev[k]&&prev[k]!==D)changed.push(lab(x.asset)+' '+x.horizon+' '+prev[k]+'→'+D);
          prev[k]=D;
        });
        const live=rows.filter(x=>x.source_gate_pass===true).length;
        document.getElementById('r21-data').textContent=rows.length+'/42 · live '+live;
        const st=document.getElementById('r21-status');
        st.innerHTML=(changed.length?'<span class="r21-pill">Изменилось: '+esc(changed.slice(0,3).join(' · '))+'</span>':'<span class="r21-pill">Сигналы без резких изменений</span>')
          +'<span class="r21-pill">обновление '+(d.at?new Date(d.at).toLocaleTimeString():'—')+'</span>';
      }

      function renderPf(d){
        const ps=Array.isArray(d?.portfolios)?d.portfolios:[];
        document.getElementById('r21-pf').innerHTML=ps.length?ps.map(p=>{
          const ret=Number(p.total_return_pct??p.latest?.total_return_pct??0);
          const dd=Number(p.drawdown_pct??p.latest?.drawdown_pct??0);
          const n=Array.isArray(p.positions)?p.positions.length:0;
          return '<div class="r21-pf"><b>'+esc(p.name)+'</b><span class="'+(ret>=0?'ok':'bad')+'">'+ret.toFixed(2)+'%</span><span>DD '+dd.toFixed(1)+'%</span><span>'+n+' поз.</span></div>';
        }).join(''):'<span class="stamp">Портфели временно недоступны</span>';
        if(ps.length){
          const rets=ps.map(p=>Number(p.total_return_pct??p.latest?.total_return_pct??0));
          const dds=ps.map(p=>Number(p.drawdown_pct??p.latest?.drawdown_pct??0));
          const open=ps.reduce((s,p)=>s+(Array.isArray(p.positions)?p.positions.length:0),0);
          document.getElementById('r21-bestret').textContent=Math.max(...rets).toFixed(2)+'%';
          document.getElementById('r21-maxdd').textContent=Math.max(...dds).toFixed(2)+'%';
          document.getElementById('r21-open').textContent=String(open);
        }
      }

      async function get(url,ms){
        const ctl=new AbortController(),tm=setTimeout(()=>ctl.abort(),ms);
        try{const r=await fetch(url,{cache:'no-store',signal:ctl.signal});if(!r.ok)throw 0;return await r.json()}finally{clearTimeout(tm)}
      }
      async function refresh(){
        install();
        const s=await get('/api/v1/signals',5000).catch(()=>null);
        if(s)renderSignals(s);
        get('/api/v1/paper-portfolios',6500).then(renderPf).catch(()=>{});
      }
      if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(refresh,300),{once:true});else setTimeout(refresh,300);
      setInterval(refresh,30000);
    })();
    </script>"""
    value=value.replace('</body>',exec_js+'</body>')
    print(json.dumps({'event':'V90_EXECUTIVE_PANEL_R21','status':'installed'},ensure_ascii=False,separators=(',',':')),flush=True)
    print(json.dumps({'event':'V90_DECISION_COCKPIT_R15','status':'installed'},ensure_ascii=False,separators=(',',':')),flush=True)
    print(json.dumps({'event':'V90_CLOSED_JOURNAL_UI_V2','status':'single_owner_no_observer'},ensure_ascii=False,separators=(',',':')),flush=True)
    return value
