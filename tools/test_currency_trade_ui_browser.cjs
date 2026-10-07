/* Optional real-browser smoke. Every request is intercepted; no service is contacted. */
const assert=require('node:assert/strict'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?path.join(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES,'playwright'):'playwright');
const {sample}=require('./test_currency_trade_ui.cjs');
const html=execFileSync('python',['-c','import veritas_currency_trade_ui as ui; print(ui.render_trading_ui())'],{encoding:'utf8'});

(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true});
  const page=await context.newPage(),requests=[],errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  let authenticated=false,d=sample();
  d.setup={required:true,can_select_account:true,can_pair_owner:true,can_confirm_owner:true,pending_owner:{user_id:123456,private_chat_id:123456,user_name:'Владелец',challenge_id:'challenge-exact'}};
  d.accounts=[{account_id:'exact-42',name:'Счёт для Currency',eligible:true},{account_id:'blocked-99',name:'Архив',eligible:false}];
  await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   requests.push({path:url.pathname,method:request.method(),body:request.postDataJSON(),headers:request.headers(),hash:url.hash,search:url.search});
   if(url.pathname==='/integrations/trading')return route.fulfill({contentType:'text/html; charset=utf-8',body:html});
   const operation=url.pathname.split('/').at(-1);let body={ok:true},status=200,headers={};
   if(operation==='session'){
    assert.equal(request.postDataJSON().setup_code,'one-time-setup');authenticated=true;
    body={authenticated:true,csrf_token:'csrf-browser-memory'};headers={'Set-Cookie':'currency_ui=test-cookie; HttpOnly; Secure; SameSite=Strict; Path=/api/v1/currency-trading/'};
   }else if(!authenticated){status=401;body={ok:false,code:'AUTH_REQUIRED'};}
   else if(operation==='dashboard')body={...d,csrf_token:'csrf-browser-memory'};
   else if(operation==='setup'&&request.postDataJSON().action==='pair_owner')body={ok:true,setup:{pairing_url:'https://t.me/AxednewsI_bot?start=vt_ABCDEF0123456789',pairing_expires_at:'2026-10-07T18:05:00Z'}};
   else if(!['action','setup'].includes(operation))throw new Error('Unexpected route '+url.pathname);
   return route.fulfill({status,contentType:'application/json',headers,body:JSON.stringify(body)});
  });
  await page.goto('https://veritas-ui.test/integrations/trading');
  await page.getByLabel('Одноразовый код',{exact:true}).waitFor({state:'visible'});
  assert.equal(await page.getByText('Текущие позиции брокера',{exact:true}).count(),0);
  await page.goto('https://veritas-ui.test/integrations/trading#setup=one-time-setup');
  await page.getByRole('heading',{name:'Текущие позиции брокера'}).waitFor();
  assert.equal(page.url(),'https://veritas-ui.test/integrations/trading');
  assert.equal(await page.getByRole('button',{name:'Подготовить предложение',exact:true}).isDisabled(),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
  assert.equal(await page.evaluate(()=>document.cookie),'');
  await page.getByLabel('Точный брокерский счёт').selectOption('exact-42');
  await page.getByRole('button',{name:'Выбрать этот счёт'}).click();
  await page.getByText('Счёт выбран. Проверьте привязку владельца.').waitFor();
  assert.deepEqual(requests.filter(r=>r.path.endsWith('/setup')).at(-1).body,{action:'bind_account',account_id:'exact-42'});
  await page.getByRole('button',{name:'Подтвердить мой Telegram'}).click();
  await page.getByText('Владелец Telegram подтверждён.').waitFor();
  assert.deepEqual(requests.filter(r=>r.path.endsWith('/setup')).at(-1).body,{action:'confirm_owner',challenge_id:'challenge-exact'});
  await page.getByRole('button',{name:'Получить код привязки'}).click();
  await page.getByRole('link',{name:'Привязать мой Telegram'}).waitFor();
  assert.match(await page.getByRole('link',{name:'Привязать мой Telegram'}).getAttribute('href'),/^https:\/\/t.me\/AxednewsI_bot\?start=vt_/);
  // Opening an owner approval is deliberately not a dashboard action.
  assert.equal(await page.locator('[data-action="approve"],[data-action="execute"]').count(),0);
  d.actions.prepare=true;d.readiness.status='READY';d.readiness.blockers=[];
  await page.getByRole('button',{name:'Обновить данные'}).click();
  await page.getByText('Данные обновлены.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Подготовить предложение',exact:true}).click();
  await page.getByText('Предложение запрошено. Подтверждение сделки — в Telegram.').waitFor();
  assert.deepEqual(requests.filter(r=>r.path.endsWith('/action')).at(-1).body,{action:'prepare'});
  for(const r of requests.filter(r=>r.method==='POST'&&!r.path.endsWith('/session')))assert.equal(r.headers['x-csrf-token'],'csrf-browser-memory');
  assert.ok(requests.every(r=>!r.search&&!r.hash));
  assert.deepEqual(errors,[]);
  await page.screenshot({path:'/tmp/veritas-currency-ui-iphone.png',fullPage:true});
  await page.setViewportSize({width:1280,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:'/tmp/veritas-currency-ui-desktop.png',fullPage:true});
  console.log('PASS: real browser at iPhone 390px and desktop 1280px; login, account binding, owner confirmation, pairing, proposal, CSRF, no overflow; all network mocked');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
