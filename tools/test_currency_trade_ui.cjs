/* Offline tests of the actual shipped renderer, HTTP client, and controller. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('veritas_currency_trade_ui.py', 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const context = vm.createContext({document:{readyState:'loading',addEventListener(){}},URLSearchParams});
vm.runInContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.testApi={renderDashboard,takeSetupCode,createClient,mount};})();'),context);
const {renderDashboard,takeSetupCode,createClient,mount} = context.testApi;
const clone = v => JSON.parse(JSON.stringify(v));
const tick = () => new Promise(resolve => setImmediate(resolve));
const sample = () => ({
  authenticated:true, status:'READY', mode:'production', paused:false, updated_at:'2026-10-07T18:00:00Z',
  connection:{status:'CONNECTED',account_id:'account-exact-42',account_name:'Выделенный счёт',broker_status:'OK',observed_at:'2026-10-07T18:00:00Z'},
  owner:{status:'BOUND',user_id:123456,private_chat_id:123456,bot_username:'AxednewsI_bot'},
  allocation:{allocation_rub:'10000',nav_rub:'9998.50',ticker:'CNYRUBF',instrument_uid:'cny-exact-uid',reconciled:true,costs_reconciled:false},
  readiness:{status:'BLOCKED',source:{primary_source:'MOEX ISS',key:'MOEX:CNYRUBF'},event_id:'cny-event-1',observed_at:'2026-10-07T17:59:45Z',blockers:['BROKER_COST_RECONCILIATION_REQUIRED']},
  positions_observed:true,positions:[{account_id:'account-exact-42',ticker:'CNYRUBF',broker_signed_lots:1,managed_signed_lots:1,blocked_lots:0,average_entry_price:'12.345',broker_observed_at:'2026-10-07T18:00:00Z',status:'RECONCILED'}],
  orders_observed:true,orders:[{status:'SUBMITTED',proposal_id:'proposal-1',account_id:'account-exact-42',action:'OPEN',direction:'LONG',lots:1,filled_lots:0,broker_order_id:'broker-1',terms:{asset:'CNYRUBF',limit_price:'12.345',source_identity:{primary_source:'MOEX ISS',key:'MOEX:CNYRUBF'}}}],
  fills_observed:true,fills:[{trade_id:'actual-fill-1',broker_order_id:'broker-prior',side:'BUY',lots:1,price:'12.331',executed_at:'2026-10-07T17:45:00Z'}],
  costs:{fees_rub:'1.50',funding_rub:null,total_rub:null,reconciled:false},
  execution_requested:true,execution_enabled:false,
  actions:{prepare:false,reconcile:true,pause:true,resume:false},setup:{required:false,can_enable_execution:true,can_disable_execution:true}
});

function rendererTests(){
  const html=renderDashboard(sample());
  for(const expected of ['Реальный счёт','account-exact-42','123456','MOEX:CNYRUBF','cny-event-1','actual-fill-1','broker-1','Принято брокером','Нужно сверить фактические комиссии'])assert.ok(html.includes(expected),expected);
  assert.match(html,/data-action="prepare" disabled/);
  assert.match(html,/data-action="reconcile">/);
  assert.match(html,/Отправка брокеру<\/dt><dd>Запрещено/);
  assert.match(html,/Разрешение владельца<\/dt><dd>Включено/);
  assert.match(html,/data-action="enable_execution">/);
  const close=renderDashboard({...sample(),orders:[{status:'PROPOSED',action:'CLOSE',direction:'LONG',terms:{side:'SELL'}}]});
  assert.match(close,/Закрытие · Продажа/);assert.doesNotMatch(close,/Закрытие · Покупка/);
  assert.match(html,/Исполнено<\/dt><dd>0 лот\./);
  assert.match(html,/Финансирование<\/dt><dd>—<\/dd>/);
  assert.match(renderDashboard({...sample(),costs:{funding_rub:'0',funding_reconciled:false}}),/Финансирование<\/dt><dd>—<\/dd>/);
  assert.doesNotMatch(html,/data-action="(?:approve|execute|arm)"/);
  const missing=renderDashboard({authenticated:true,mode:'disabled'});
  for(const expected of ['Нужна настройка','Не настроено','Не привязан','Свежий снимок позиций пока недоступен','Журнал заявок пока недоступен','Данные исполнений пока недоступны'])assert.ok(missing.includes(expected),expected);
  assert.doesNotMatch(missing,/Открытых позиций по CNYRUBF нет|Подключён|Журнал.*пуст/);
  assert.match(missing,/Комиссии<\/dt><dd>—<\/dd>/);
  const empty=renderDashboard({...sample(),positions:[],orders:[],fills:[]});
  assert.match(empty,/Открытых позиций по CNYRUBF нет/);
  assert.match(empty,/Подтверждённых исполнений пока нет/);
  const states=['PROPOSED','AWAITING_APPROVAL','APPROVED','SENDING','PARTIALLY_FILLED','FILLED','REJECTED','SEND_UNKNOWN'];
  for(const state of states)assert.ok(renderDashboard({...sample(),orders:[{...sample().orders[0],status:state}]}).includes(state==='SEND_UNKNOWN'?'Отправка требует сверки':({PROPOSED:'Предложение',AWAITING_APPROVAL:'Ждёт подтверждения',APPROVED:'Подтверждено владельцем',SENDING:'Отправляется брокеру',PARTIALLY_FILLED:'Частично исполнено',FILLED:'Исполнено',REJECTED:'Отклонено'})[state]));
  const resumed=renderDashboard({...sample(),paused:true,actions:{resume:true}});
  assert.match(resumed,/data-action="resume">Возобновить предложения/);
  assert.doesNotMatch(resumed,/data-action="pause"/);
}

function xssTests(){
  const attack='"><img src=x onerror=alert(1)><script>evil()</script>&\'';
  const d=sample();
  d.connection={status:attack,account_id:attack,account_name:attack};
  d.owner={status:attack,user_id:attack,private_chat_id:attack,bot_username:attack};
  d.readiness={status:attack,source:{primary_source:attack,key:attack},event_id:attack,blockers:[{code:attack,message:attack}]};
  d.orders=[{proposal_id:attack,status:attack,account_id:attack,reason_code:attack,terms:{source_identity:{primary_source:attack},asset:attack}}];
  d.positions=[{ticker:attack,account_id:attack}];
  d.fills=[{trade_id:attack,broker_order_id:attack,side:attack}];
  d.accounts=[{account_id:attack,name:attack,eligible:true}];
  d.setup={required:true,can_select_account:true,can_pair_owner:true,pairing_code:attack,pairing_url:'javascript:alert(1)',can_confirm_owner:true,pending_owner:{user_name:attack,user_id:attack,private_chat_id:attack,challenge_id:attack}};
  d.callback_nonce='MUST_NOT_RENDER_CALLBACK_NONCE';d.terms_signature='MUST_NOT_RENDER_SIGNATURE';d.access_token='MUST_NOT_RENDER_TOKEN';
  const html=renderDashboard(d);
  assert.doesNotMatch(html,/<img|<script|javascript:|MUST_NOT_RENDER/);
  assert.match(html,/&lt;img/);
  assert.match(html,/&quot;/);
  assert.doesNotMatch(html,/<a href="https:\/\/t.me/);
  const pairing={...sample(),setup:{required:true,pairing_url:'https://t.me/AxednewsI_bot?start=vt_ABCDEF0123456789',pairing_expires_at:'2026-10-07T18:05:00Z'}};
  assert.match(renderDashboard(pairing),/href="https:\/\/t.me\/AxednewsI_bot\?start=vt_ABCDEF0123456789"/);
  const instructions=renderDashboard({...pairing,setup:{...pairing.setup,can_pair_owner:true}});
  assert.match(instructions,/эту же вкладку кабинета/);
  assert.match(instructions,/«Обновить данные».*«Подтвердить мой Telegram»/);
  assert.match(source,/Код для первого входа передан в вашей персональной ссылке настройки/);
  assert.match(source,/До привязки бот код доступа не присылает/);
  for(const url of ['https://t.me.evil.test/bot?start=vt_ABCDEF012345','//t.me/bot?start=vt_ABCDEF012345','https://t.me/bot?start=vt_ABCDEF012345&leak=x','https://evil.test/'])assert.doesNotMatch(renderDashboard({...pairing,setup:{...pairing.setup,pairing_url:url}}),/<a href="https:/);
}

async function clientTests(){
  const history=[];
  const code=takeSetupCode({hash:'#setup=one%2Btime',pathname:'/integrations/trading',search:'?other=x'},{replaceState(...args){history.push(args);}});
  assert.equal(code,'one+time');assert.equal(history[0][2],'/integrations/trading');
  const calls=[];
  const client=createClient(async(url,options)=>{calls.push({url,options});return {ok:true,status:200,json:async()=>({authenticated:true,csrf_token:'csrf-memory-only'})};});
  await client.login('one+time');await client.dashboard();await client.setup({action:'bind_account',account_id:'exact'});await client.action('prepare');
  assert.deepEqual(calls.map(x=>x.url),['session','dashboard','setup','action'].map(x=>'/api/v1/currency-trading/'+x));
  assert.equal(calls[0].options.body,JSON.stringify({setup_code:'one+time'}));
  assert.equal(calls[1].options.method,'GET');assert.equal(calls[1].options.body,undefined);
  assert.equal(calls[2].options.headers['X-CSRF-Token'],'csrf-memory-only');
  for(const {url,options} of calls){assert.doesNotMatch(url,/one\+time|\?/);assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');assert.equal(options.referrerPolicy,'no-referrer');}
  client.clear();await client.action('pause');assert.equal(calls.at(-1).options.headers['X-CSRF-Token'],'');
  const unauthorized=createClient(async()=>({ok:false,status:401,json:async()=>({code:'UI_SESSION_EXPIRED'})}));
  await assert.rejects(unauthorized.dashboard(),e=>e.status===401&&e.code==='UI_SESSION_EXPIRED');
  const offline=createClient(async()=>{throw new Error('must not leak transport internals');});
  await assert.rejects(offline.dashboard(),e=>e.code==='NETWORK_ERROR'&&!e.message.includes('internals'));
}

function fakeDom(){
  const elements=new Map(),doc={readyState:'complete',buttons:[],getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},querySelectorAll(){return doc.buttons;}};
  function element(id){return {id,hidden:false,value:'',textContent:'',className:'',listeners:{},setAttribute(){},addEventListener(event,handler){this.listeners[event]=handler;},get innerHTML(){return this.html||'';},set innerHTML(html){this.html=html;if(id==='dashboard')doc.buttons=[...html.matchAll(/<button\b([^>]*)>/g)].map(m=>({disabled:/\bdisabled/.test(m[1]),dataset:{action:/data-action="([^"]+)"/.exec(m[1])?.[1]}}));}};}
  return doc;
}

async function controllerTests(){
  const calls=[],d=sample();
  const api={clear(){calls.push(['clear']);},async dashboard(){calls.push(['dashboard']);return clone(d);},async login(code){calls.push(['login',code]);return {authenticated:true};},async setup(body){calls.push(['setup',clone(body)]);return {setup:{pairing_url:'https://t.me/AxednewsI_bot?start=vt_ABCDEF0123456789'}};},async action(key){calls.push(['action',key]);return {ok:true};}};
  const doc=fakeDom(),history=[];
  const ui=mount(doc,{location:{hash:'#setup=single-use-code',pathname:'/integrations/trading'},history:{replaceState(...args){history.push(args);}}},api);
  await tick();assert.deepEqual(calls,[['dashboard']]);assert.equal(history[0][2],'/integrations/trading');assert.equal(doc.getElementById('login').hidden,true);
  // Rendering a fresh response while locked must preserve the server's disabled capabilities.
  assert.equal(doc.buttons.find(b=>b.dataset.action==='prepare').disabled,true);
  await ui.action('prepare');await ui.action('approve');await ui.action('execute');assert.equal(calls.filter(c=>c[0]==='action').length,0);
  await ui.action('enable_execution');assert.deepEqual(calls.filter(c=>c[0]==='action').at(-1),['action','enable_execution']);
  await ui.action('disable_execution');assert.deepEqual(calls.filter(c=>c[0]==='action').at(-1),['action','disable_execution']);
  await ui.action('reconcile');assert.deepEqual(calls.filter(c=>c[0]==='action').at(-1),['action','reconcile']);
  d.setup={required:true,can_select_account:true,can_pair_owner:true,can_confirm_owner:true,pending_owner:{user_id:123456,private_chat_id:123456,user_name:'Me',challenge_id:'exact-challenge'}};
  d.accounts=[{account_id:'exact-account',name:'Mine',eligible:true},{account_id:'ineligible-account',eligible:false}];
  await ui.refresh();doc.getElementById('account-id').value='ineligible-account';await ui.selectAccount();assert.equal(calls.filter(c=>c[0]==='setup').length,0);
  doc.getElementById('account-id').value='exact-account';await ui.selectAccount();assert.deepEqual(calls.find(c=>c[0]==='setup'),['setup',{action:'bind_account',account_id:'exact-account'}]);
  await ui.action('confirm_owner');assert.deepEqual(calls.filter(c=>c[0]==='setup').at(-1),['setup',{action:'confirm_owner',challenge_id:'exact-challenge'}]);
  await ui.action('pair_owner');assert.match(doc.getElementById('dashboard').innerHTML,/start=vt_ABCDEF0123456789/);
  d.paused=true;d.actions.resume=true;await ui.refresh();await ui.action('resume');assert.deepEqual(calls.filter(c=>c[0]==='action').at(-1),['action','resume']);
  // Duplicate taps do not duplicate an action while its request is pending.
  let release;api.action=key=>{calls.push(['delayed',key]);return new Promise(resolve=>{release=resolve;});};
  const first=ui.action('reconcile');await ui.action('reconcile');assert.equal(calls.filter(c=>c[0]==='delayed').length,1);release({ok:true});await first;
  api.dashboard=async()=>{throw Object.assign(new Error(),{status:401,code:'UI_SESSION_EXPIRED'});};await ui.refresh();assert.equal(doc.getElementById('login').hidden,false);assert.equal(doc.getElementById('dashboard').innerHTML,'');assert.match(doc.getElementById('feedback').textContent,/Сессия истекла/);
  // A server outage leaves the last account facts visible with an explicit failure notice.
  api.dashboard=async()=>clone(d);await ui.refresh();api.dashboard=async()=>{throw {code:'NETWORK_ERROR'};};await ui.refresh();assert.match(doc.getElementById('dashboard').innerHTML,/account-exact-42/);assert.match(doc.getElementById('feedback').textContent,/устаревшими/);
}

async function loginRecoveryTests(){
  const code='private-one-time-setup',json=(status,data)=>({ok:status>=200&&status<300,status,json:async()=>clone(data)});
  const dashboard=()=>json(200,{...sample(),csrf_token:'csrf-from-current-session'});
  function scenario(handler,hash='#setup='+code){
    const requests=[],history=[],doc=fakeDom();
    const client=createClient(async(url,options)=>{const request={operation:url.split('/').at(-1),url,options};requests.push(request);return handler(request,requests);});
    const ui=mount(doc,{location:{hash,pathname:'/integrations/trading'},history:{replaceState(...args){history.push(args);}}},client);
    return {requests,history,doc,ui,operations:()=>requests.map(r=>r.operation),retry:async()=>{doc.getElementById('retry-login').listeners.click();await tick();}};
  }
  // An already-consumed link must keep a valid browser session and its real CSRF token.
  const existing=scenario(({operation,options})=>{
    if(operation==='dashboard')return dashboard();
    assert.equal(operation,'action');assert.equal(options.headers['X-CSRF-Token'],'csrf-from-current-session');return json(200,{ok:true});
  });
  await tick();assert.deepEqual(existing.operations(),['dashboard']);assert.equal(existing.doc.getElementById('login').hidden,true);
  assert.equal(existing.history[0][2],'/integrations/trading');assert.equal(existing.doc.getElementById('setup-code').value,'');
  assert.doesNotMatch(existing.doc.getElementById('dashboard').innerHTML+existing.doc.getElementById('feedback').textContent,new RegExp(code));
  await existing.ui.action('reconcile');assert.deepEqual(existing.operations(),['dashboard','action','dashboard']);

  // Fresh and expired sessions both require a server-confirmed 401 before exactly one code POST.
  for(const missing of ['AUTHENTICATION_REQUIRED','SESSION_EXPIRED']){
    let authenticated=false;
    const fresh=scenario(({operation,options})=>{
      if(operation==='dashboard')return authenticated?dashboard():json(401,{code:missing});
      assert.equal(operation,'session');assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{setup_code:code});
      authenticated=true;return json(200,{authenticated:true,csrf_token:'new-session-csrf'});
    });
    await tick();assert.deepEqual(fresh.operations(),['dashboard','session','dashboard']);assert.equal(fresh.doc.getElementById('login').hidden,true);
    assert.equal(fresh.doc.getElementById('retry-login').hidden,true);
  }

  // A network outage before checking the cookie cannot consume the code. An explicit retry keeps it in this tab.
  let offline=true,authenticated=false;
  const before=scenario(({operation,options})=>{
    if(offline)throw new Error('offline');
    if(operation==='dashboard')return authenticated?dashboard():json(401,{code:'AUTHENTICATION_REQUIRED'});
    assert.equal(operation,'session');assert.deepEqual(JSON.parse(options.body),{setup_code:code});authenticated=true;return json(200,{authenticated:true});
  });
  await tick();assert.deepEqual(before.operations(),['dashboard']);assert.equal(before.doc.getElementById('retry-login').hidden,false);
  assert.match(before.doc.getElementById('feedback').textContent,/Код повторно вводить не нужно/);assert.equal(before.doc.getElementById('setup-code').value,'');
  offline=false;await before.retry();assert.deepEqual(before.operations(),['dashboard','dashboard','session','dashboard']);assert.equal(before.doc.getElementById('login').hidden,true);

  // If a login response is lost, retry checks the cookie: it only resends the code when the server still reports no session.
  for(const cookieInstalled of [false,true]){
    let authenticated=false,attempts=0;
    const lost=scenario(({operation,options})=>{
      if(operation==='dashboard')return authenticated?dashboard():json(401,{code:'AUTHENTICATION_REQUIRED'});
      assert.equal(operation,'session');assert.deepEqual(JSON.parse(options.body),{setup_code:code});attempts++;
      if(attempts===1){authenticated=cookieInstalled;throw new Error('response lost');}
      authenticated=true;return json(200,{authenticated:true});
    });
    await tick();assert.equal(lost.doc.getElementById('retry-login').hidden,false);assert.equal(attempts,1);
    await lost.retry();assert.equal(lost.doc.getElementById('login').hidden,true);assert.equal(attempts,cookieInstalled?1:2);
    assert.deepEqual(lost.operations(),cookieInstalled?['dashboard','session','dashboard']:['dashboard','session','dashboard','session','dashboard']);
  }

  // Once POST succeeds, a dashboard outage never causes the already-consumed code to be sent again.
  for(const cookieSurvives of [true,false]){
    let loginSucceeded=false,firstRead=true;
    const after=scenario(({operation})=>{
      if(operation==='session'){loginSucceeded=true;return json(200,{authenticated:true});}
      assert.equal(operation,'dashboard');
      if(!loginSucceeded)return json(401,{code:'AUTHENTICATION_REQUIRED'});
      if(firstRead){firstRead=false;return json(503,{code:'CONSOLE_TEMPORARILY_UNAVAILABLE'});}
      return cookieSurvives?dashboard():json(401,{code:'SESSION_EXPIRED'});
    });
    await tick();assert.equal(after.doc.getElementById('retry-login').hidden,false);
    await after.retry();assert.deepEqual(after.operations(),['dashboard','session','dashboard','dashboard']);
    assert.equal(after.doc.getElementById('login').hidden,cookieSurvives);
  }

  // Permission failures, unrelated 401s and malformed success payloads are not proof that a session is missing.
  for(const [status,payload] of [[403,{code:'ACCESS_FORBIDDEN'}],[401,{code:'UNKNOWN_AUTH_ERROR'}],[200,{ok:true}],[200,null],[401,null],[200,[]]]){
    const blocked=scenario(()=>json(status,payload));await tick();assert.deepEqual(blocked.operations(),['dashboard']);
    assert.equal(blocked.doc.getElementById('login').hidden,false);
    assert.doesNotMatch(blocked.doc.getElementById('feedback').textContent,new RegExp(code));
    if(payload===null||Array.isArray(payload)||status===200){assert.equal(blocked.doc.getElementById('retry-login').hidden,false);assert.match(blocked.doc.getElementById('feedback').textContent,/Код повторно вводить не нужно/);}
  }
  // None of the recovery requests puts the code into a URL or refers it to another origin.
  for(const r of [...existing.requests,...before.requests]){assert.equal(r.options.credentials,'same-origin');assert.equal(r.options.referrerPolicy,'no-referrer');assert.doesNotMatch(r.url,/setup=|\?/);}
}

module.exports={sample};
if(require.main===module)(async()=>{rendererTests();xssTests();await clientTests();await controllerTests();await loginRecoveryTests();console.log('PASS: Currency console states, exact binding, escaping, session transport, capabilities, duplicate-tap handling, and one-time login recovery');})().catch(error=>{console.error(error);process.exitCode=1;});
