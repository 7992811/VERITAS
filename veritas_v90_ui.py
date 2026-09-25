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
    value = value.replace('30 ячеек · ~','35 ячеек · ~').replace('6 активов × 5 ТФ','7 активов × 5 ТФ').replace('6/6 активов','7/7 активов')
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
    value = value.replace('Последние сделки','Закрытые сделки · CLOSED_FINAL').replace('ПОСЛЕДНИЕ СДЕЛКИ','ЗАКРЫТЫЕ СДЕЛКИ · CLOSED_FINAL')
    value = value.replace("${p.name==='Champion'?'70%+':'77%+'}","${p.badge||''}")
    value = value.replace('Шаг позиции 5% · gross ≤ 2,5× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.',
                          'Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · риск по стопу 1–2% NAV · hard stop DD 8–12% в зависимости от мандата.')
    
    replacement = """posel.innerHTML=positions.length?positions.map(z=>`<div class="assetview position-card"><div class="assetview-head position-head"><b>${z.portfolio} · ${z.asset}</b><b class="${z.direction==='LONG'?'ok':'bad'}">${z.direction} · ${(100*Number(z.target_fraction||0)).toFixed(0)}%</b></div><div class="position-columns"><div class="position-col position-left"><div><span>Вход</span><b>${Number(z.avg_entry_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Текущая</span><b>${Number(z.last_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div class="position-gap"><span>Объём ₽</span><b>${rub(z.notional_rub)}</b></div><div><span>Объём $</span><b>${z.notional_usd==null?'—':Number(z.notional_usd).toLocaleString('en-US',{maximumFractionDigits:0})+' USD'}</b></div><div><span>Кол-во</span><b>${['BTC','ETH'].includes(z.asset)?Number(z.units||0).toFixed(4):Math.round(Number(z.units||0)).toLocaleString('ru-RU')}</b></div></div><div class="position-col position-right"><div><span>P/L</span><b class="${Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad'}">${rub(z.unrealized_pnl_rub)} · ${z.unrealized_return_pct==null?'—':Number(z.unrealized_return_pct).toFixed(2)+'%'}</b></div><div><span>SL</span><b>${z.stop_price==null?'—':Number(z.stop_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>TP</span><b>${z.take_price==null?'—':Number(z.take_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Prob-ty</span><b title="${z.probability_source||'—'}">${z.entry_probability==null?'—':(100*Number(z.entry_probability)).toFixed(1)+'%'+(['EMPIRICAL_CALIBRATION','CALIBRATED_PROBABILITY'].includes(z.probability_source)?' · calibr.':' · model')}</b></div><div><span>Time</span><b>${z.opened_at?new Date(z.opened_at).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'}</b></div></div></div></div>`).join(''):'Открытых позиций нет — портфели в cash.';const trades="""
    
    pattern = r"""posel\.innerHTML=positions\.length\?positions\.map\(z=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Открытых позиций[^']*';const trades="""
    value, count = re.subn(pattern, replacement, value, count=1, flags=re.S)
    if count != 1:
        print(json.dumps({'event':'V90_UI_PATCH','status':'error','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    else:
        print(json.dumps({'event':'V90_UI_PATCH','status':'ok','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    
    trade_replacement = """trel.innerHTML=trades.length?(()=>{const order=['Champion','Challenger','Impulse','Aggressive'];const groups={};trades.slice(0,80).forEach(t=>{const k=t.portfolio_name||'—';(groups[k]||(groups[k]=[])).push(t)});const keys=Object.keys(groups).sort((a,b)=>{const ia=order.indexOf(a),ib=order.indexOf(b);return (ia<0?999:ia)-(ib<0?999:ib)||a.localeCompare(b)});const fmtTime=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';const fmtHold=x=>x==null?'—':(Number(x)>=3600?(Number(x)/3600).toFixed(1)+' ч':Math.max(1,Math.round(Number(x)/60))+' мин');const fmtPx=x=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:3});return keys.map(name=>{const rows=groups[name];const wins=rows.filter(t=>Number(t.net_pnl_rub||0)>0).length;const net=rows.reduce((a,t)=>a+Number(t.net_pnl_rub||0),0);const wr=rows.length?100*wins/rows.length:0;return `<div class="closed-portfolio"><div class="closed-portfolio-summary"><b>${name}</b><span>· ${rows.length} закрыто</span><span>· ${wins} прибыльных</span><span>· win rate ${wr.toFixed(1)}%</span><span>· Net P&L <b class="${net>=0?'ok':'bad'}">${rub(net)}</b></span></div><div class="closed-list">${rows.map(t=>{const pnl=Number(t.net_pnl_rub||0);const prob=t.entry_probability==null?'—':(100*Number(t.entry_probability)).toFixed(1)+'% ('+(['EMPIRICAL_CALIBRATION','CALIBRATED_PROBABILITY'].includes(t.probability_source)?'calibr.':'model')+')';return `<div class="assetview closed-trade-card"><div class="closed-trade-head"><b>${t.asset||'—'} · ${t.direction||'—'}${t.recovered?' · RECOVERED':''}</b><b class="${pnl>=0?'ok':'bad'}">P&L ${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}${t.return_pct==null?'':' · '+Number(t.return_pct).toFixed(2)+'%'}</b></div><div class="closed-row"><span>Вход <b>${fmtPx(t.avg_entry_price)}</b></span><span>· Выход <b>${fmtPx(t.avg_exit_price)}</b></span><span>· Gross <b>${t.gross_pnl_rub==null?'—':rub(t.gross_pnl_rub)}</b></span></div><div class="closed-row closed-costs"><span>Комиссия <b>${rub(t.fees_rub||0)}</b></span><span>· Фандинг <b>${rub(t.funding_rub||0)}</b></span><span>· MFE <b>${t.mfe_pct==null?'—':Number(t.mfe_pct).toFixed(2)+'%'}</b></span><span>· MAE <b>${t.mae_pct==null?'—':Number(t.mae_pct).toFixed(2)+'%'}</b></span><span>· Giveback <b>${t.giveback_pct==null?'—':Number(t.giveback_pct).toFixed(2)+'%'}</b></span><span>· Причина <b>${t.exit_reason||'—'}</b></span></div><div class="closed-row closed-time"><span>Открыта <b>${fmtTime(t.opened_at)}</b></span><span>· Закрыта <b>${fmtTime(t.closed_at)}</b></span><span>· Hold <b>${fmtHold(t.held_seconds)}</b></span><span>· QTY <b>${t.quantity==null?'—':Number(t.quantity).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></span><span>· SL/TP <b>${fmtPx(t.stop_price)} / ${fmtPx(t.take_price)}</b></span><span>· ${t.horizon||'—'}${t.setup?' · '+t.setup:''}${t.regime?' · '+t.regime:''}</span></div><div class="closed-learning"><span class="learn-dot">●</span><span>Вывод для обучения:</span><b>${t.learning_label||'—'}</b><span>${t.learning_conclusion||'—'}</span></div><div class="closed-prob"><span class="prob-dot">●</span><span>Entry Prob-ty:</span><b title="${t.probability_source||'—'}">${prob}</b></div></div>`}).join('')}</div></div>`}).join('')})():'Закрытых сделок пока нет.'"""
    trade_pattern = r"""trel\.innerHTML=trades\.length\?trades\.slice\(0,30\)\.map\(t=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Сделок в журнале пока нет\.'"""
    value, trade_count = re.subn(trade_pattern, trade_replacement, value, count=1, flags=re.S)
    print(json.dumps({'event':'V90_CLOSED_TRADE_UI_PATCH','replacements':trade_count,
                      'status':'ok' if trade_count==1 else 'error'},ensure_ascii=False,separators=(',',':')),flush=True)
    if trade_count != 1:
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
       refreshClosed();setInterval(refreshClosed,30000);
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
    #portfoliopositions .position-head b{font-size:15px;line-height:1.1}
    #portfoliopositions .position-columns{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,.95fr);gap:18px;width:100%}
    #portfoliopositions .position-col{display:flex;flex-direction:column;gap:3px;min-width:0}
    #portfoliopositions .position-col>div{display:grid;grid-template-columns:64px minmax(0,1fr);align-items:baseline;column-gap:7px;white-space:nowrap;min-width:0}
    #portfoliopositions .position-col span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.2px}
    #portfoliopositions .position-col b{font-size:12px;line-height:1.15;overflow:hidden;text-overflow:ellipsis;text-align:left}
    #portfoliopositions .position-left .position-gap{margin-top:7px}
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
     #portfoliopositions .position-head b{font-size:13px}
     #portfoliopositions .position-columns{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:12px}
     #portfoliopositions .position-col{gap:2px}
     #portfoliopositions .position-col>div{grid-template-columns:53px minmax(0,1fr);column-gap:5px}
     #portfoliopositions .position-col span{font-size:8px}
     #portfoliopositions .position-col b{font-size:10.5px}
     #portfoliopositions .position-left .position-gap{margin-top:6px}
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
      const label=a=>a==='NQ'?'NQ Futures':a;
      const oldRender=window.renderMatrix;
      if(typeof oldRender==='function'){
        window.renderMatrix=function(rows){
          oldRender(rows);
          document.querySelectorAll('#matrix .asset-name').forEach(el=>{
            if(el.textContent.trim()==='NQ')el.textContent='NQ Futures';
          });
          document.querySelectorAll('#superstrip .superasset').forEach(el=>{
            el.childNodes.forEach(n=>{
              if(n.nodeType===Node.TEXT_NODE && n.textContent.trim()==='NQ')n.textContent='NQ Futures';
            });
          });
        };
      }
      const observer=new MutationObserver(()=>{
        document.querySelectorAll('.asset-name,.assetview-name,.superasset,b').forEach(el=>{
          const t=el.textContent.trim();
          if(t==='NQ')el.textContent='NQ Futures';
          else if(t.startsWith('NQ ·'))el.textContent=t.replace(/^NQ\s*·/,'NQ Futures ·');
          else if(t.includes('· NQ ·'))el.textContent=t.replace('· NQ ·','· NQ Futures ·');
        });
      });
      observer.observe(document.body,{subtree:true,childList:true});
    })();
    </script>"""
    value = value.replace('</body>', nq_label_js + '</body>')
    fast_signal_js = r"""<script id="V90_FAST_SIGNAL_FEED">
    (function(){
      async function v90LoadSignals(){
        try{
          const ctl=new AbortController();
          const tm=setTimeout(()=>ctl.abort(),6000);
          const r=await fetch('/api/v1/signals',{cache:'no-store',signal:ctl.signal});
          clearTimeout(tm);
          if(!r.ok)throw new Error('signals HTTP '+r.status);
          const d=await r.json();
          const rows=Array.isArray(d.signals)?d.signals:[];
          if(typeof renderMatrix==='function')renderMatrix(rows);
          const stamp=document.getElementById('stamp');
          if(stamp){
            const at=d.at?new Date(d.at):new Date();
            stamp.textContent='сигналы '+at.toLocaleString()+' · '+rows.length+'/35';
          }
        }catch(e){
          const ms=document.getElementById('matrixstatus');
          if(ms && !ms.textContent.trim())ms.textContent='обновление сигналов…';
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
    return value
