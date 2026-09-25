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
          <img class="veritas-logo-img" src="/assets/veritas-logo-source.webp?v=90.2" onerror="this.onerror=null;this.src='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAMAAABg3Am1AAABWGlDQ1BJQ0MgUHJvZmlsZQAAeJx9kLFLw1AQxr9WpaB1EB0cHDKJQ5SSCro4tBVEcQhVweqUvqapkMZHkiIFN/+Bgv+BCs5uFoc6OjgIopPo5uSk4KLleS+JpCJ6j+N+fO+74zggOW5wbvcDqDu+W1zKK5ulLSX1jAS9IAzm8Zyur0r+rj/j/T703k7LWb///43Biukxqp+UGcZdH0ioxPqezyXvE4+5tBRxS7IV8onkcsjngWe9WCC+JlZYzagQvxCr5R7d6uG63WDRDnL7tOlsrMk5lBNYxA48cNgw0IQCHdk//LOBv4BdcjfhUp+FGnzqyZEiJ5jEy3DAMAOVWEOGUpN3ju53F91PjbWDJ2ChI4S4iLWVDnA2Rydrx9rUPDAyBFy1ueEagdRHmaxWgddTYLgEjN5Qz7ZXzWrh9uk8MPAoxNskkDoEui0hPo6E6B5T8wNw6XwBA6diE8HYWhMAAADAUExURWFocJudoZOZn9XY3dPW2FZfZ6asspOZoGZpbmdtdNXY3C4zN7a8wbzBxhMdJ3yCiX2EiuHj57Oz1jxETnh9hHV8g3qCi2xsoG2TlnN6gRUVcTU+R7bJzLzBxDE7RT9/f39//7vEu7vBxgAA/zhCSn///7q+w8W5xQAAADA6RDpETf7+/igyPFBaYyIsNRslL1pkbUlTXEJMVX9/f3F6g2NsdWlye4qTmnqEjIKLk1VVVZqjqqmpqbK1uZObo6SqscnPPe8AAABAdFJOU6AlWvYcz+qbIGFaBPj3/aJhohWwcJvQCyLKA9gWXq0EAh6sAdgCcxYA/fsH/fr9/fz7+wP7+vv6+/wD+wUv/PzLTB5VAAAFLklEQVR42n1W6XqjOBCUQIDP2LmTuWf2EhICSdzGmPd/q63GniST+Xb1wwa7ir6qu2Hy/dGllCLohBBS3vz2r2Tv7ssPoAQPdX0TXq807sv/I9DDCd5Y243XYRjOlON/EfQTPnhQW2ubqr+5DrfbTfz5u5Tro/6dUB7oM2DGeWPrqjvxVbiNp2EakpH+Oei3hG+zo2IfeZUqZ2zTD/EKLsXTqe37LgnE/MzyQjiu8f0pYD7NslR5Z5ouDoRYIYZ4OPVVU9f1buas1/psQe9ZlGZ5nqVpqkyTBLN9vrrebE5911hjnIvYnn7VDDmPPqZZUeRZRhbMPcH5HxqfQvyIh66qrfH0qI+3e0EWgugChwXHKEQ+W9CcAktOZALBZUWu7hBDEpwpcEhFS/E2IZJLPcKDuDZKpWl0pyVnzNfIm7gzqYoosMW315R/lfqZHUkoz8abOwG4Slma11XyCfUlP9Zvq1rKMTJLeSjJOzzshqnsKmPqytdNn8xZ+0U3pQwq55lcn4sq7l1a5FewUOS2afr4n3cygwgfrJ8JpAMRKaSluFJM5YXqqqpN5qS86OogxxuGWC8EfU+JzYng8qwwfdcNf8tXEwgn6APmEOPFO3BVWhSFZxHlH4Jpp8+Sv+B1UjWUk5lQSl57r3xe5EUETcCUO7XtsBHyrxl/XIuksjVnEBcjvHh23lPMRGApMarh1MY/9Fwy5IRX1tgnpByEdal3xjvnIIc8Z+xeEUMNONuVREEWckxAcGbBUH8GflID7kgMeAALfErxtPE0xeGKwhgfGl6jtAuWEwHlgFiNI3yeLlngFJ1djBOG4qiDykbceuUWt8gK1Nxaa5xtSG15umfcKo+IWLDZbrfhNT3PgKBSfyHEHeRtdiwnQasFO0aGTsOTkM4qaNAs3CAsEK6y5epUYYhEXM394jjTOyTE2irg8czQ90ZF3KGXFl+uitvV1KFHbbCcCSlDxyWNreumSuTZRAjVRNxTfLdXf4rt0FeVZRI1oWTiAk4DXnWtvpi4PpqPIBRp8EXx63Bou8pwbQif+iUIY0f4vh3l3ZmxWkZPviiyRbRchZup7e1ScjUTDJes1HFH+CGBgADfxNsV5wqEZSBwO01VhOGiqFqKrkqZtMCfhljLRQt8HG/0o7oq8oUMQwzLyT5JHc14dEeJnhVt3+KcMA2WUzhNw5CICMrnSTzF8bZCsTnJwXsTyCPuRAytYiQmcsGjaTqdwOVptvw8tKfTdIIbs0fQ33wpjzIYgO+6GGOAdxNd96vlUrQduYqIpYhmvE2gRIZuGQfCN1UgF5L1GI5V02sZI3lVX5Nc92cDNZ8JaFiEXaGeD7rU4pnwdZMkNdWzNiMGG3qGhvQOnU4ELseW8KYOqHubDuPaVLWDYKxbwiqKQCFb+nue3miRqiZJMl0eqZuhZ0tNZhzTx1JSUyrl7smAPDe5qIAxlLYbKWhnGevghPIjTQCqslKocvlzA6F4NXaAV0zLRxmQMTuXlnKoWUay8Dt5kK8EETkMBuWQqIOA4I0hdSLvCIqEjWshy9eliJ/ReGjtiEzwhgh5rihKHdFmynxwmXOXLbqQO0c7gJy4oTFhsoJGzFHuMY7Qy+zs0AsBFaA5l2ZupDXyYF1eOHJC+PN0AeDdniZFpjTq9IGKYYpsL7+uNY0azDvxMnhf3gSOWB/A5+mdfFzDqYKKIgM4VBTR+DqoX18dnsCgFaA4nPre4OtRjn7G858B/PqucUBysaxzZGohg53kH8ihgvx5s8n+Bf7iRUTQ/eFwAAAAAElFTkSuQmCC'" alt="VERITAS logo">
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
    brand_fix_css = """<style id="V90_BRAND_VISUAL_FIX">
    .veritas-brandlock{display:flex!important;flex-direction:column!important;align-items:flex-start!important;width:max-content!important;max-width:100%!important}
    .veritas-primary{display:flex!important;flex-direction:column!important;align-items:stretch!important;min-width:0!important}
    .veritas-core{display:flex!important;align-items:flex-start!important;gap:16px!important;min-width:0!important}
    .veritas-right{display:flex!important;flex-direction:column!important;align-items:flex-start!important;min-width:0!important}
    .veritas-logo-img{width:88px!important;height:100px!important;flex:0 0 88px!important;display:block!important;object-fit:contain!important;object-position:center!important}
    .veritas-wordmark{height:66px!important;font-size:54px!important;font-weight:460!important;letter-spacing:.155em!important;color:#edf1f4!important;text-shadow:0 0 12px rgba(255,255,255,.07)!important}
    .veritas-wordmark .wm-e{height:.94em!important;width:.72em!important;margin-right:.10em!important}
    .veritas-wordmark .wm-e i{height:.145em!important;background:#91bda3!important}
    .markets-word{margin-top:4px!important;font-size:22px!important;font-weight:470!important;letter-spacing:.13em!important;color:#b5c0c9!important;opacity:1!important;text-shadow:0 0 10px rgba(181,192,201,.08)!important}
    .veritas-subtitle{margin-top:11px!important;color:#aeb8c1!important;font-size:12px!important;font-weight:540!important;letter-spacing:.06em!important;line-height:1.15!important;white-space:nowrap!important;text-align:left!important;text-align-last:auto!important;display:flex!important;justify-content:space-between!important;gap:18px!important}
    .veritas-subtitle::after{display:none!important;content:none!important}
    @media(max-width:900px){
      .veritas-core{gap:11px!important}
      .veritas-logo-img{width:64px!important;height:74px!important;flex-basis:64px!important}
      .veritas-wordmark{height:50px!important;font-size:38px!important;font-weight:470!important;letter-spacing:.12em!important}
      .markets-word{font-size:16px!important;font-weight:480!important;color:#bcc6ce!important}
      .veritas-subtitle{margin-top:7px!important;font-size:8.8px!important;font-weight:560!important;gap:10px!important}
    }
    </style>"""
    value = value.replace('</head>', brand_fix_css + '</head>')
    brand_fix_js = r"""<script id="V90_BRAND_WIDTH_FIX">
    (function(){
      function fitBrand(){
        const core=document.querySelector('.veritas-core');
        const sub=document.querySelector('.veritas-subtitle');
        if(!core||!sub)return;
        if(!sub.dataset.v90Split){
          sub.innerHTML='<span>Цифровой</span><span>Инвестиционный</span><span>Комитет</span>';
          sub.dataset.v90Split='1';
        }
        const w=Math.ceil(core.getBoundingClientRect().width);
        if(w>0)sub.style.width=w+'px';
      }
      if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>requestAnimationFrame(fitBrand),{once:true});
      else requestAnimationFrame(fitBrand);
      window.addEventListener('resize',fitBrand,{passive:true});
      setTimeout(fitBrand,500);
    })();
    </script>"""
    value = value.replace('</body>', brand_fix_js + '</body>')

    
    value = value.replace('Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Champion, Challenger, Impulse и Aggressive. Реальные деньги не используются.',
                          'Четыре независимых модельных paper-портфеля по 1 000 000 ₽: Импульсный, Агрессивный, Чемпион и Челленджер. Реальные деньги не используются.')
    value = value.replace('V86','V90').replace('v86','v90')
    return value
