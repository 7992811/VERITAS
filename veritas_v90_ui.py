"""VERITAS Markets 9.0 UI layer.

Ports the last approved 86.x interface/branding onto the 9.0 runtime.
This module changes presentation only; trading logic and persistence remain in v9.0.
"""
import json
import re

UI_VERSION = 'veritas-ui-v9.0-from-v86.3-approved'

def apply_v90_ui(html):
    value = str(html)
    value = value.replace('Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.',
                          'Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Champion, Challenger, Impulse и Aggressive. Реальные деньги не используются.')
    value = value.replace('Открытых позиций нет — оба портфеля в cash.','Открытых позиций нет — портфели в cash.')
    value = value.replace('30 ячеек · ~','35 ячеек · ~').replace('6 активов × 5 ТФ','7 активов × 5 ТФ').replace('6/6 активов','7/7 активов')
    value = value.replace('NDX','NQ')
    # V86.2 BRAND_AND_HEADER_REFINEMENT
    _brand_markup = '''<div class="veritas-brandlock">
      <div class="veritas-primary">
        <div class="veritas-core">
          <img class="veritas-logo-img" src="data:image/webp;base64,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
    Rt0l4PIfnmiSsfljeunkBtqw8u5XfUUdxNI/6Lg7DiEwrzpmc4fuvGcHZhJ9
    UApw0Zsu3k4G6yMXC5WSdPTrSOGMVS94Z7I2Y74tM3jiX+8EktM3DRh47I37
    oFZ/AVQUXelHycMRuP9PTnjtaF5xzJ7Y8fojc6U5oo/KlBi+/p1bz8G9z3hZ
    +rY3YNRQ8P7POqQwfzgJPvdrHTDRf6UMm9/VTZTeT0pJd2UkamlxdNVb37yr
    mWTzgkTi6Op3b80bCxL9KKRWPM73fuBconT1B4nUO/CrNy41amyv/zn3lLx2
    3nH3f/vgEBVinrYmYAYbPvT77t6JOs3cPbv7T982zhyX4tCy1mySiG/7S/fa
    VSeJ5Mf/5F3rwRDzvAUY+8QfdiIZ6XTKqXb/4zcHUFexalQlMKr2bKAGDH/u
    n3iWvDbyDHZ++xEgCDH/KwJ3Xnr7BqiDaiN3P/aoQRBdVgKyimiBKXRlMrNN
    n97XBdVCSnSPPvhyYsPEQikLaP9DN61rk1UPial//NLtCia6lA2svOjGb2xW
    Il7/m9+8qzGMupgxRv7nrx8i83gqc2bWf+gOGkGxYt7XLIApwd03X7cCr4GT
    sfe+LUM06FYCDvygL/wiafFEfMr9pT/4xv1YUFcoZmw5MY0plpSYHHvdRxaS
    BGbznRmi0kwQSPDgR9vIFUsyzj24liUDxoyaFqC56RP/0unow0YV/HQ96e7/
    +YFRaHQFhbHwqY2kwsOosJzpu96xCZLzJDmAhmw2QPj4j38kGUURx4uM7S99
    yyiB2UMFRlj1ZMfda39nA6ug9Zte55Tc/+INQtYVlI3uxjsmaaAQUoKpO+//
    Qosk8WRo7Ut+9elHm/Qo/Ldvf+m9Mzm4QkiJcv3HTzlOl82GVY0LvuK/JrxO
    nvSOKpQNLgUZzn7ueONjDJTeDRDjrpcel7qFVI1KGn78mz96PoSKhVHkR26E
    /b92Qt2BBGz/xPcmoyb2RZIX0rGvuTcQRbeTde4MX3M74BHA6OtgxnSRzaY/
    /fa9BV0rYNVNP/C5llR4JZ5lnP+pG4BmEAujyO/ZSCcsOz5F7zKD0UfftGcI
    d6kPXkijH/5mCIGuDZZdvXIDGRnTc42XM3VnAKPM4M637onBZrMGag8Y3/Ky
    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
    PzbY1LQBLJagTRUNCATE" alt="VERITAS logo">
          <div class="veritas-right">
            <div class="veritas-wordmark">
              <span class="wm-v">V</span><span class="wm-e" aria-label="E"><i></i><i></i><i></i></span><span>RITAS</span>
            </div>
            <div class="markets-word">Markets</div>
          </div>
        </div>
        <div class="veritas-subtitle">Цифровой Инвестиционный Комитет</div>
      </div>
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
       const initial=rows.slice(0,5), hidden=Math.max(0,rows.length-initial.length);
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
    
    value = value.replace('Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Champion, Challenger, Impulse и Aggressive. Реальные деньги не используются.',
                          'Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Импульсный, Агрессивный, Чемпион и Челленджер. Реальные деньги не используются.')
    value = value.replace('V86','V90').replace('v86','v90')
    return value
